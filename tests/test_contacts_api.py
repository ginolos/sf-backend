from sqlalchemy import inspect

from app.database import SessionLocal, engine
from app.models import Address


BASE = "/api/v1/contacts"
TINY_PNG = "data:image/png;base64,iVBORw0KGgo="


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "sqlite"


def test_create_contact(client, payload):
    response = client.post(BASE, json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["id"] > 0
    assert body["email"] == "ada@example.com"
    assert body["full_name"] == "Ada Lovelace"
    assert body["created_at"] and body["updated_at"]


def test_photo_is_stored_and_returned(client, payload):
    response = client.post(BASE, json={**payload, "photo": TINY_PNG})
    assert response.status_code == 201
    contact_id = response.json()["id"]
    assert response.json()["photo"] == TINY_PNG
    assert client.get(f"{BASE}/{contact_id}").json()["photo"] == TINY_PNG


def test_contact_owns_multiple_typed_addresses(client, payload):
    addresses = [
        {
            "type": "Home",
            "street_address": "12 Home Lane",
            "city": "London",
            "state": None,
            "postal_code": "SW1A 1AA",
            "country": "UK",
        },
        {
            "type": "Work",
            "street_address": "1 Market St",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94105",
            "country": "USA",
        },
    ]
    body = client.post(BASE, json={**payload, "addresses": addresses}).json()

    assert [address["type"] for address in body["addresses"]] == ["Home", "Work"]
    assert all(address["id"] > 0 for address in body["addresses"])
    assert [address["street_address"] for address in body["addresses"]] == [
        "12 Home Lane",
        "1 Market St",
    ]


def test_addresses_table_has_contact_foreign_key(client):
    foreign_keys = inspect(engine).get_foreign_keys("addresses")
    contact_key = next(key for key in foreign_keys if key["referred_table"] == "contacts")
    assert contact_key["constrained_columns"] == ["contact_id"]
    assert contact_key["referred_columns"] == ["id"]
    assert contact_key["options"].get("ondelete") == "CASCADE"


def test_address_requires_valid_type_and_location(client, payload):
    invalid_type = client.post(
        BASE,
        json={**payload, "addresses": [{"type": "Vacation", "city": "Paris"}]},
    )
    blank = client.post(
        BASE,
        json={**payload, "email": "blank@example.com", "addresses": [{"type": "Other"}]},
    )
    assert invalid_type.status_code == 422
    assert blank.status_code == 422


def test_invalid_photo_data_is_rejected(client, payload):
    response = client.post(BASE, json={**payload, "photo": "data:text/plain;base64,aGVsbG8="})
    assert response.status_code == 422


def test_arbitrary_bytes_with_an_image_label_are_rejected(client, payload):
    response = client.post(BASE, json={**payload, "photo": "data:image/png;base64,aGVsbG8="})
    assert response.status_code == 422


def test_bytes_must_match_the_declared_image_type(client, payload):
    jpeg_bytes = "data:image/png;base64,/9j/2Q=="
    response = client.post(BASE, json={**payload, "photo": jpeg_bytes})
    assert response.status_code == 422


def test_create_requires_valid_email(client, payload):
    response = client.post(BASE, json={**payload, "email": "not-an-email"})
    assert response.status_code == 422


def test_create_requires_names(client, payload):
    response = client.post(BASE, json={**payload, "first_name": ""})
    assert response.status_code == 422


def test_duplicate_email_conflicts(client, payload):
    assert client.post(BASE, json=payload).status_code == 201
    response = client.post(BASE, json={**payload, "email": "ADA@example.com"})
    assert response.status_code == 409


def test_get_contact(client, payload):
    contact_id = client.post(BASE, json=payload).json()["id"]
    response = client.get(f"{BASE}/{contact_id}")
    assert response.status_code == 200
    assert response.json()["id"] == contact_id


def test_get_missing_contact_returns_404(client):
    assert client.get(f"{BASE}/9999").status_code == 404


def test_list_pagination_and_total(client, payload):
    for index in range(5):
        client.post(BASE, json={**payload, "email": f"user{index}@example.com"})

    response = client.get(BASE, params={"limit": 2, "offset": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["limit"] == 2 and body["offset"] == 2


def test_list_search(client, payload):
    client.post(BASE, json=payload)
    client.post(
        BASE,
        json={**payload, "first_name": "Grace", "last_name": "Hopper", "email": "grace@example.com", "company": "US Navy"},
    )

    hits = client.get(BASE, params={"search": "hopper"}).json()
    assert hits["total"] == 1
    assert hits["items"][0]["last_name"] == "Hopper"

    by_company = client.get(BASE, params={"search": "navy"}).json()
    assert by_company["total"] == 1

    misses = client.get(BASE, params={"search": "nobody"}).json()
    assert misses["total"] == 0


def test_list_sorting(client, payload):
    client.post(BASE, json={**payload, "last_name": "Zhang", "email": "z@example.com"})
    client.post(BASE, json={**payload, "last_name": "Adams", "email": "a@example.com"})

    names = [
        item["last_name"]
        for item in client.get(BASE, params={"sort_by": "last_name", "order": "asc"}).json()["items"]
    ]
    assert names == ["Adams", "Zhang"]


def test_list_rejects_bad_sort_field(client):
    assert client.get(BASE, params={"sort_by": "; DROP TABLE contacts"}).status_code == 422


def test_patch_updates_only_sent_fields(client, payload):
    created = client.post(BASE, json=payload).json()
    contact_id = created["id"]
    response = client.patch(f"{BASE}/{contact_id}", json={"phone": "+1-000-000-0000"})
    assert response.status_code == 200
    body = response.json()
    assert body["phone"] == "+1-000-000-0000"
    assert body["first_name"] == "Ada"
    assert body["company"] == "Analytical Engines"
    assert body["addresses"] == created["addresses"]


def test_patch_atomically_replaces_and_clears_addresses(client, payload):
    contact_id = client.post(BASE, json=payload).json()["id"]
    replacement = [{"type": "Other", "city": "Paris", "country": "France"}]

    replaced = client.patch(f"{BASE}/{contact_id}", json={"addresses": replacement})
    assert replaced.status_code == 200
    assert len(replaced.json()["addresses"]) == 1
    assert replaced.json()["addresses"][0]["type"] == "Other"
    assert replaced.json()["addresses"][0]["city"] == "Paris"

    cleared = client.patch(f"{BASE}/{contact_id}", json={"addresses": None})
    assert cleared.status_code == 200
    assert cleared.json()["addresses"] == []


def test_patch_duplicate_email_conflicts(client, payload):
    first = client.post(BASE, json=payload).json()["id"]
    client.post(BASE, json={**payload, "email": "grace@example.com"})
    response = client.patch(f"{BASE}/{first}", json={"email": "grace@example.com"})
    assert response.status_code == 409


def test_patch_same_email_is_allowed(client, payload):
    contact_id = client.post(BASE, json=payload).json()["id"]
    response = client.patch(f"{BASE}/{contact_id}", json={"email": payload["email"]})
    assert response.status_code == 200


def test_put_replaces_contact(client, payload):
    contact_id = client.post(BASE, json=payload).json()["id"]
    response = client.put(
        f"{BASE}/{contact_id}",
        json={"first_name": "Grace", "last_name": "Hopper", "email": "grace@example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["full_name"] == "Grace Hopper"
    assert body["company"] is None  # omitted fields are cleared by PUT


def test_put_keeps_photo_when_it_is_carried_through(client, payload):
    contact_id = client.post(BASE, json={**payload, "photo": TINY_PNG}).json()["id"]
    response = client.put(
        f"{BASE}/{contact_id}",
        json={**payload, "photo": TINY_PNG, "company": "Updated"},
    )
    assert response.status_code == 200
    assert response.json()["photo"] == TINY_PNG
    assert response.json()["addresses"][0]["type"] == "Work"


def test_put_replaces_the_entire_address_collection(client, payload):
    contact_id = client.post(BASE, json=payload).json()["id"]
    response = client.put(
        f"{BASE}/{contact_id}",
        json={
            **payload,
            "addresses": [{"type": "Home", "street_address": "99 New Address"}],
        },
    )

    assert response.status_code == 200
    assert len(response.json()["addresses"]) == 1
    assert response.json()["addresses"][0]["street_address"] == "99 New Address"


def test_put_missing_contact_returns_404(client):
    response = client.put(
        f"{BASE}/9999",
        json={"first_name": "A", "last_name": "B", "email": "ab@example.com"},
    )
    assert response.status_code == 404


def test_delete_contact(client, payload):
    created = client.post(BASE, json=payload).json()
    contact_id = created["id"]
    address_ids = [address["id"] for address in created["addresses"]]
    assert client.delete(f"{BASE}/{contact_id}").status_code == 204
    assert client.get(f"{BASE}/{contact_id}").status_code == 404
    assert client.delete(f"{BASE}/{contact_id}").status_code == 404
    with SessionLocal() as db:
        assert all(db.get(Address, address_id) is None for address_id in address_ids)


def test_root_lists_entrypoints(client):
    body = client.get("/").json()
    assert body["contacts"] == BASE
