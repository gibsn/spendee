import base64
import json
from uuid import uuid4

from requests import Session

from .exceptions import SpendeeError


class SpendeeFirestoreError(SpendeeError):
    """Raised when the modern Spendee Firestore backend rejects a request."""


def _decode_jwt_payload(token):
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        return json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (IndexError, TypeError, ValueError):
        raise SpendeeFirestoreError("Firebase returned an invalid ID token")


def _decode_value(value):
    for field in (
        "stringValue",
        "timestampValue",
        "booleanValue",
        "integerValue",
        "doubleValue",
        "referenceValue",
    ):
        if field in value:
            raw = value[field]
            return int(raw) if field == "integerValue" else raw
    if "nullValue" in value:
        return None
    if "mapValue" in value:
        return {
            key: _decode_value(item)
            for key, item in value["mapValue"].get("fields", {}).items()
        }
    if "arrayValue" in value:
        return [_decode_value(item) for item in value["arrayValue"].get("values", [])]
    raise SpendeeFirestoreError("Unsupported Firestore value: {!r}".format(value))


def _encode_value(value):
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, dict):
        return {
            "mapValue": {
                "fields": {
                    key: _encode_value(item)
                    for key, item in value.items()
                }
            }
        }
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_encode_value(item) for item in value]}}
    raise TypeError("Unsupported Firestore value type: {}".format(type(value).__name__))


def _decode_document(document):
    result = {
        key: _decode_value(value)
        for key, value in document.get("fields", {}).items()
    }
    result["_name"] = document["name"]
    result["_id"] = document["name"].rsplit("/", 1)[-1]
    return result


class FirestoreLabelsMixin(object):
    """Modern Spendee labels stored in the application's Firestore project."""

    _firestore_project_id = "spendee-app"
    _firestore_timeout = 30

    @property
    def _firestore_documents_url(self):
        return (
            "https://firestore.googleapis.com/v1/projects/{}/"
            "databases/(default)/documents"
        ).format(self._firestore_project_id)

    @property
    def _firestore_documents_name(self):
        return "projects/{}/databases/(default)/documents".format(
            self._firestore_project_id
        )

    @property
    def firestore_user_id(self):
        if not self._access_token:
            self.user_login()
        claims = _decode_jwt_payload(self._access_token)
        if claims.get("aud") != self._firestore_project_id:
            raise SpendeeFirestoreError(
                "Firebase ID token belongs to another project"
            )
        user_id = claims.get("user_uuid") or claims.get("sub")
        if not user_id:
            raise SpendeeFirestoreError(
                "Firebase ID token has no user identifier"
            )
        return str(user_id)

    def _firestore_request(self, method, url, retry_auth=True, **kwargs):
        if not self._access_token:
            self.user_login()
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = "Bearer {}".format(self._access_token)
        response = Session.request(
            self,
            method=method,
            url=url,
            headers=headers,
            timeout=self._firestore_timeout,
            **kwargs
        )
        if response.status_code == 401 and retry_auth:
            self.user_login()
            return self._firestore_request(
                method,
                url,
                retry_auth=False,
                **kwargs
            )
        if not response.ok:
            raise SpendeeFirestoreError(
                "Firestore request failed with HTTP {}: {}".format(
                    response.status_code,
                    response.text[:500],
                ),
                response=response,
            )
        return response

    def _firestore_collection(self, path):
        documents = []
        page_token = None
        while True:
            params = {"pageSize": 300}
            if page_token:
                params["pageToken"] = page_token
            response = self._firestore_request(
                "GET",
                "{}/{}".format(
                    self._firestore_documents_url,
                    path.strip("/"),
                ),
                params=params,
            )
            payload = response.json()
            documents.extend(
                _decode_document(item)
                for item in payload.get("documents", [])
            )
            page_token = payload.get("nextPageToken")
            if not page_token:
                return documents

    def list_labels(self):
        """Return modern Spendee labels as ``id``/``name`` dictionaries."""

        labels = self._firestore_collection(
            "users/{}/labels".format(self.firestore_user_id)
        )
        return sorted(
            [
                {
                    "id": item["_id"],
                    "name": str(item.get("text") or item.get("name") or ""),
                }
                for item in labels
            ],
            key=lambda label: label["name"].casefold(),
        )

    def list_firestore_wallets(self):
        """Return modern wallet UUIDs, including their legacy numeric IDs."""

        wallets = self._firestore_collection(
            "users/{}/wallets".format(self.firestore_user_id)
        )
        return [
            {
                "id": wallet["_id"],
                "legacy_id": wallet.get("legacyId"),
                "name": wallet.get("name"),
                "currency": wallet.get("currency"),
                "status": wallet.get("status"),
                "type": wallet.get("type"),
            }
            for wallet in wallets
        ]

    def resolve_firestore_wallet_id(self, legacy_wallet_id):
        matches = [
            wallet["id"]
            for wallet in self.list_firestore_wallets()
            if wallet.get("legacy_id") == legacy_wallet_id
        ]
        if len(matches) != 1:
            raise SpendeeFirestoreError(
                "Expected one Firestore wallet for legacy ID {}, found {}".format(
                    legacy_wallet_id,
                    len(matches),
                )
            )
        return matches[0]

    def _transaction_labels_path(self, wallet_id, transaction_id):
        return (
            "users/{}/wallets/{}/transactions/{}/transactionLabels"
        ).format(
            self.firestore_user_id,
            wallet_id,
            transaction_id,
        )

    def get_transaction_labels(
        self,
        wallet_id,
        transaction_id,
        resolve_names=True,
    ):
        """Return label names or IDs attached to a modern transaction."""

        relations = self._firestore_collection(
            self._transaction_labels_path(wallet_id, transaction_id)
        )
        label_ids = [
            str(relation["label"])
            for relation in relations
            if relation.get("label")
        ]
        if not resolve_names:
            return sorted(label_ids)
        names = {
            label["id"]: label["name"]
            for label in self.list_labels()
        }
        return sorted(
            [names.get(label_id, label_id) for label_id in label_ids],
            key=str.casefold,
        )

    def _resolve_label_names(self, requested):
        labels = self.list_labels()
        exact = {label["name"]: label["id"] for label in labels}
        by_id = {label["id"]: label["name"] for label in labels}
        folded = {}
        for label in labels:
            folded.setdefault(label["name"].casefold(), []).append(label)

        resolved = {}
        for raw_name in requested:
            if not isinstance(raw_name, str):
                raise ValueError("label names must be strings")
            name = raw_name.strip()
            if not name:
                raise ValueError("label names must not be empty")
            label_id = exact.get(name)
            if label_id is None:
                candidates = folded.get(name.casefold(), [])
                if len(candidates) == 1:
                    label_id = candidates[0]["id"]
            if label_id is None:
                raise ValueError(
                    "Unknown or ambiguous Spendee label: {}".format(name)
                )
            resolved[label_id] = by_id[label_id]
        return resolved

    def _set_transaction_label_ids(
        self,
        wallet_id,
        transaction_id,
        requested,
    ):
        path = self._transaction_labels_path(wallet_id, transaction_id)
        current_documents = self._firestore_collection(path)
        current = {
            str(document["label"]): document["_name"]
            for document in current_documents
            if document.get("label")
        }
        add_ids = sorted(set(requested) - set(current))
        remove_ids = sorted(set(current) - set(requested))

        if not add_ids and not remove_ids:
            return {
                "changed": False,
                "labels": sorted(requested.values(), key=str.casefold),
                "added": [],
                "removed": [],
            }

        user_id = self.firestore_user_id
        transaction_name = (
            "{}/users/{}/wallets/{}/transactions/{}"
        ).format(
            self._firestore_documents_name,
            user_id,
            wallet_id,
            transaction_id,
        )
        writes = []
        for label_id in add_ids:
            relation_id = str(uuid4())
            relation_name = "{}/transactionLabels/{}".format(
                transaction_name,
                relation_id,
            )
            fields = {
                "label": label_id,
                "author": user_id,
                "modelVersion": 1,
                "path": {
                    "user": user_id,
                    "wallet": wallet_id,
                    "transaction": transaction_id,
                    "transactionLabel": relation_id,
                },
            }
            writes.append(
                {
                    "update": {
                        "name": relation_name,
                        "fields": {
                            key: _encode_value(value)
                            for key, value in fields.items()
                        },
                    },
                    "updateTransforms": [
                        {
                            "fieldPath": "createdAt",
                            "setToServerValue": "REQUEST_TIME",
                        }
                    ],
                }
            )
        writes.extend(
            {"delete": current[label_id]}
            for label_id in remove_ids
        )
        writes.append(
            {
                "transform": {
                    "document": transaction_name,
                    "fieldTransforms": [
                        {
                            "fieldPath": "updatedAt",
                            "setToServerValue": "REQUEST_TIME",
                        }
                    ],
                }
            }
        )
        self._firestore_request(
            "POST",
            "{}:commit".format(self._firestore_documents_url),
            json={"writes": writes},
        )

        all_labels = {
            label["id"]: label["name"]
            for label in self.list_labels()
        }
        return {
            "changed": True,
            "labels": sorted(requested.values(), key=str.casefold),
            "added": sorted(
                [all_labels.get(label_id, label_id) for label_id in add_ids],
                key=str.casefold,
            ),
            "removed": sorted(
                [all_labels.get(label_id, label_id) for label_id in remove_ids],
                key=str.casefold,
            ),
        }

    def set_transaction_labels(self, wallet_id, transaction_id, labels):
        """Replace a modern transaction's labels with an exact set."""

        requested = self._resolve_label_names(labels)
        return self._set_transaction_label_ids(
            wallet_id,
            transaction_id,
            requested,
        )

    def set_legacy_transaction_labels(
        self,
        legacy_wallet_id,
        transaction_uuid,
        labels,
    ):
        """Set modern labels using IDs returned by the legacy REST API."""

        requested = self._resolve_label_names(labels)
        wallet_id = self.resolve_firestore_wallet_id(legacy_wallet_id)
        return self._set_transaction_label_ids(
            wallet_id,
            transaction_uuid,
            requested,
        )

    def _prepare_legacy_transaction_labels(self, legacy_wallet_id, labels):
        return {
            "wallet_id": self.resolve_firestore_wallet_id(legacy_wallet_id),
            "labels": self._resolve_label_names(labels),
        }

    @staticmethod
    def _find_transaction_uuid(value):
        if not isinstance(value, dict):
            return None
        for key in ("uuid", "transaction_uuid"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for key in ("transaction", "result", "data"):
            candidate = FirestoreLabelsMixin._find_transaction_uuid(
                value.get(key)
            )
            if candidate:
                return candidate
        return None
