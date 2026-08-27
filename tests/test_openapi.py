"""Guards on the generated OpenAPI document, so the docs can't silently rot."""

import pytest

CONTACTS_PATH = "/api/v1/contacts"
ITEM_PATH = f"{CONTACTS_PATH}/{{contact_id}}"
HTTP_METHODS = ("get", "post", "put", "patch", "delete")


@pytest.fixture
def spec(client) -> dict:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def _operations(spec: dict):
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method in HTTP_METHODS:
                yield path, method, operation


def test_spec_is_served_and_versioned(spec):
    assert spec["openapi"].startswith("3.")


def test_docs_uis_are_served(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_info_block_is_populated(spec):
    info = spec["info"]
    assert info["title"] == "Contacts API"
    assert info["version"]
    assert "in-process SQLite" in info["description"]
    assert info["summary"]
    assert info["contact"]["url"].endswith("/sf-backend")
    assert info["license"]["name"] == "MIT"


def test_tags_have_descriptions(spec):
    tags = {tag["name"]: tag.get("description", "") for tag in spec["tags"]}
    assert set(tags) == {"contacts", "meta"}
    assert all(len(description) > 20 for description in tags.values())


def test_every_operation_is_documented(spec):
    for path, method, operation in _operations(spec):
        where = f"{method.upper()} {path}"
        assert operation.get("summary"), f"{where} is missing a summary"
        assert len(operation.get("description", "")) > 20, f"{where} is missing a description"
        assert operation.get("tags"), f"{where} is missing tags"
        assert operation.get("operationId"), f"{where} is missing an operationId"
        assert operation["responses"], f"{where} documents no responses"
        for status_code, response in operation["responses"].items():
            assert response.get("description"), f"{where} {status_code} is missing a description"


def test_operation_ids_are_stable_and_unique(spec):
    ids = [operation["operationId"] for _, _, operation in _operations(spec)]
    assert len(ids) == len(set(ids))
    assert set(ids) == {
        "createContact",
        "listContacts",
        "getContact",
        "replaceContact",
        "updateContact",
        "deleteContact",
        "healthCheck",
        "getRoot",
    }


def test_all_endpoints_are_present(spec):
    assert set(spec["paths"][CONTACTS_PATH]) == {"get", "post"}
    assert set(spec["paths"][ITEM_PATH]) == {"get", "put", "patch", "delete"}
    assert "/health" in spec["paths"]


@pytest.mark.parametrize(
    ("path", "method", "status_code"),
    [
        (CONTACTS_PATH, "post", "409"),
        (ITEM_PATH, "get", "404"),
        (ITEM_PATH, "put", "404"),
        (ITEM_PATH, "put", "409"),
        (ITEM_PATH, "patch", "404"),
        (ITEM_PATH, "patch", "409"),
        (ITEM_PATH, "delete", "404"),
        (ITEM_PATH, "delete", "204"),
        (CONTACTS_PATH, "post", "201"),
    ],
)
def test_error_and_success_responses_are_declared(spec, path, method, status_code):
    responses = spec["paths"][path][method]["responses"]
    assert status_code in responses, f"{method.upper()} {path} does not document {status_code}"
    assert responses[status_code]["description"]


def test_error_responses_reference_the_error_schema(spec):
    content = spec["paths"][ITEM_PATH]["get"]["responses"]["404"]["content"]["application/json"]
    assert content["schema"]["$ref"].endswith("/ErrorResponse")
    assert content["example"]["detail"]


def test_validation_errors_are_documented(spec):
    assert "422" in spec["paths"][ITEM_PATH]["get"]["responses"]
    assert "HTTPValidationError" in spec["components"]["schemas"]


def test_list_query_parameters_are_described(spec):
    params = {p["name"]: p for p in spec["paths"][CONTACTS_PATH]["get"]["parameters"]}
    assert set(params) >= {"search", "limit", "offset", "sort_by", "order"}
    for name, param in params.items():
        assert param.get("description"), f"query param {name} is missing a description"
    assert params["limit"]["schema"]["maximum"] == 200
    assert "created_at" in params["sort_by"]["description"]


def test_path_parameter_is_described(spec):
    param = spec["paths"][ITEM_PATH]["get"]["parameters"][0]
    assert param["name"] == "contact_id"
    assert param["description"]


def test_contact_fields_are_described_and_have_examples(spec):
    schema = spec["components"]["schemas"]["ContactRead"]
    assert schema["description"]
    for name, field in schema["properties"].items():
        assert field.get("description"), f"ContactRead.{name} is missing a description"
    assert schema["properties"]["email"]["examples"] == ["ada@example.com"]
    assert schema["properties"]["full_name"]["description"]
    assert schema["properties"]["addresses"]["items"]["$ref"].endswith("/AddressRead")


def test_address_schema_has_foreign_record_shape(spec):
    address = spec["components"]["schemas"]["AddressRead"]
    assert set(address["required"]) >= {"id", "type"}
    assert address["properties"]["type"]["$ref"].endswith("/AddressType")


def test_request_bodies_carry_examples(spec):
    create = spec["components"]["schemas"]["ContactCreate"]
    assert len(create["examples"]) == 2
    assert create["examples"][0]["email"] == "ada@example.com"
    assert set(create["required"]) == {"first_name", "last_name", "email"}

    patch = spec["components"]["schemas"]["ContactUpdate"]
    assert patch["examples"]
    assert "required" not in patch  # every field on PATCH is optional


def test_put_and_patch_semantics_are_explained(spec):
    assert "cleared" in spec["paths"][ITEM_PATH]["put"]["description"]
    assert "omit" in spec["paths"][ITEM_PATH]["patch"]["description"]
