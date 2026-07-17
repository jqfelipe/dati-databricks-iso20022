import os
import unittest
from unittest.mock import patch

from app import app, build_blob_uri, create_metadata


VALID_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.09">
  <CstmrCdtTrfInitn><PmtInf><CdtTrfTxInf /></PmtInf></CstmrCdtTrfInitn>
</Document>"""


class Iso20022ValidationTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_creates_validated_metadata(self):
        metadata = create_metadata(
            "payment.xml", VALID_XML, "abfss://inbox@storage.dfs.core.windows.net/payment.xml",
            "CC-123", "API",
        )

        self.assertEqual(metadata.status, "VALIDATED")
        self.assertEqual(metadata.transaction_count, 1)
        self.assertEqual(len(metadata.content_hash), 64)

    def test_rejects_non_iso_namespace(self):
        with patch.dict(
            os.environ,
            {"ABFSS_BASE_URI": "abfss://inbox@storage.dfs.core.windows.net/inbound/"},
        ), patch("app.read_abfss_file", return_value=b"<Document><Message /></Document>"):
            response = self.client.post(
                "/api/files/validate",
                data={"file_name": "payment.xml", "client_id": "CC-123", "channel": "API"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["status"], "FAILED")

    def test_builds_uri_from_base_path_and_file_name(self):
        with patch.dict(
            os.environ,
            {"ABFSS_BASE_URI": "abfss://inbox@storage.dfs.core.windows.net/inbound/"},
        ):
            uri = build_blob_uri("payment.xml")

        self.assertEqual(
            uri,
            "abfss://inbox@storage.dfs.core.windows.net/inbound/payment.xml",
        )


if __name__ == "__main__":
    unittest.main()
