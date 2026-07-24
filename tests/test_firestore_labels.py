import base64
import json
import unittest

from spendee import Spendee


def jwt(payload):
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return "header.{}.signature".format(encoded)


def document(document_name, **fields):
    encoded = {}
    for key, value in fields.items():
        if isinstance(value, int):
            encoded[key] = {"integerValue": str(value)}
        else:
            encoded[key] = {"stringValue": value}
    return {"name": document_name, "fields": encoded}


class Response(object):
    def __init__(self, payload):
        self.status_code = 200
        self.ok = True
        self.text = json.dumps(payload)
        self._payload = payload

    def json(self):
        return self._payload


class FakeSpendee(Spendee):
    def __init__(self):
        super(FakeSpendee, self).__init__("person@example.com", "secret")
        self._access_token = jwt(
            {
                "aud": "spendee-app",
                "sub": "firebase-user",
                "user_uuid": "spendee-user",
            }
        )
        self.rest_calls = []
        self.commit_payload = None
        root = (
            "projects/spendee-app/databases/(default)/documents/"
            "users/spendee-user"
        )
        self.collections = {
            "users/spendee-user/labels": [
                document(
                    "{}/labels/taxi-id".format(root),
                    text="такси",
                ),
                document(
                    "{}/labels/family-id".format(root),
                    text="семейное",
                ),
            ],
            "users/spendee-user/wallets": [
                document(
                    "{}/wallets/wallet-uuid".format(root),
                    name="Операционка",
                    legacyId=2899807,
                    currency="RUB",
                    status="active",
                )
            ],
            (
                "users/spendee-user/wallets/wallet-uuid/transactions/"
                "transaction-uuid/transactionLabels"
            ): [],
        }

    def _firestore_request(self, method, url, retry_auth=True, **kwargs):
        if url.endswith(":commit"):
            self.commit_payload = kwargs["json"]
            return Response({"writeResults": []})
        for suffix, documents in self.collections.items():
            if url.endswith(suffix):
                return Response({"documents": documents})
        raise AssertionError("Unexpected Firestore request: {} {}".format(method, url))

    def post(self, url, version="v1", **kwargs):
        self.rest_calls.append(
            {
                "url": url,
                "version": version,
                "json": kwargs.get("json"),
            }
        )
        return {"id": 99, "uuid": "transaction-uuid"}


class FirestoreLabelsTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeSpendee()

    def test_lists_labels_and_resolves_legacy_wallet(self):
        self.assertEqual(
            [label["name"] for label in self.client.list_labels()],
            ["семейное", "такси"],
        )
        self.assertEqual(
            self.client.resolve_firestore_wallet_id(2899807),
            "wallet-uuid",
        )

    def test_create_transaction_validates_and_writes_labels(self):
        result = self.client.create_transaction(
            wallet_id=2899807,
            category_id=20,
            amount=-500,
            note="Taxi",
            labels=["такси", "семейное"],
        )

        self.assertEqual(len(self.client.rest_calls), 1)
        self.assertEqual(
            result["firestore_labels"]["labels"],
            ["семейное", "такси"],
        )
        self.assertIsNotNone(self.client.commit_payload)
        writes = self.client.commit_payload["writes"]
        self.assertEqual(len(writes), 3)
        self.assertEqual(
            {
                writes[0]["update"]["fields"]["label"]["stringValue"],
                writes[1]["update"]["fields"]["label"]["stringValue"],
            },
            {"taxi-id", "family-id"},
        )

    def test_unknown_label_is_rejected_before_transaction_creation(self):
        with self.assertRaisesRegex(ValueError, "Unknown or ambiguous"):
            self.client.create_transaction(
                wallet_id=2899807,
                category_id=20,
                amount=-500,
                labels=["неизвестно"],
            )

        self.assertEqual(self.client.rest_calls, [])


if __name__ == "__main__":
    unittest.main()
