"""Databricks App for validating and registering ISO 20022 XML files."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET

from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from azure.storage.filedatalake import DataLakeServiceClient
from databricks import sql
from defusedxml import ElementTree as DefusedET
from flask import Flask, jsonify, render_template_string, request
from lxml import etree

app = Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

MAX_FILE_SIZE_BYTES = int(os.getenv("MAX_FILE_SIZE_BYTES", 20 * 1024 * 1024))
TABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")
ALLOWED_STATUSES = {"RECEIVED", "VALIDATED", "PROCESSING", "FAILED", "PROCESSED"}
ISO20022_ROOTS = {
    "Document",
    "BusinessApplicationHeader",
    "BusinessApplicationHeaderV01",
}
PRIMARY_TRANSACTION_ELEMENTS = {
    "CdtTrfTxInf",
    "DrctDbtTxInf",
    "TxInfAndSts",
}

PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>ISO 20022 Validator</title>
<style>body{font-family:Arial,sans-serif;max-width:760px;margin:3rem auto;color:#172b4d}label{display:block;margin-top:1rem;font-weight:bold}input{width:100%;padding:.55rem;box-sizing:border-box}button{margin-top:1.5rem;padding:.65rem 1rem;background:#1468ce;color:#fff;border:0;border-radius:4px}small{color:#52606d}</style>
</head><body><h1>Validador ISO 20022</h1><p>Valida XML de forma segura y registra sus metadatos.</p>
<form action="/api/files/validate" method="post" enctype="multipart/form-data">
<label>Nombre del archivo XML <input type="text" name="file_name" placeholder="payment.xml" required></label>
<label>ID cliente <input type="text" name="client_id" placeholder="TipoId-Id" required></label>
<label>Canal <input type="text" name="channel" placeholder="WEB, API, SFTP..." required></label>
<button type="submit">Validar y registrar</button></form>
<p><small>El archivo se obtiene de la ruta configurada en ABFSS_BASE_URI. La persistencia se habilita configurando DATABRICKS_SQL_WAREHOUSE_ID y FILE_METADATA_TABLE.</small></p>
</body></html>"""


@dataclass(frozen=True)
class FileMetadata:
    unique_identifier: str
    file_name: str
    file_size: int
    blob_uri: str
    upload_date: str
    status: str
    client_id: str
    channel: str
    transaction_count: int
    process_date: str
    content_hash: str
    message_type: str
    validation_errors: list[str]


class ValidationError(ValueError):
    """Raised when an uploaded file cannot be accepted."""


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def validate_text(value: str, field_name: str, max_length: int = 512) -> str:
    value = value.strip()
    if not value:
        raise ValidationError(f"{field_name} es obligatorio.")
    if len(value) > max_length or any(ord(char) < 32 for char in value):
        raise ValidationError(f"{field_name} contiene caracteres no permitidos.")
    return value


def validate_blob_uri(uri: str) -> str:
    uri = validate_text(uri, "blob_uri", 2048)
    if not re.match(r"^(abfss?|https?)://", uri, re.IGNORECASE):
        raise ValidationError("blob_uri debe usar los esquemas abfs, abfss, http o https.")
    return uri


def validate_file_name(file_name: str) -> str:
    if "/" in file_name or "\\" in file_name:
        raise ValidationError("El nombre del archivo no puede incluir rutas.")
    safe_name = validate_text(file_name, "file_name", 255)
    if not safe_name.lower().endswith(".xml"):
        raise ValidationError("El nombre del archivo debe ser un archivo .xml sin rutas.")
    return safe_name


def build_blob_uri(file_name: str) -> str:
    base_uri = validate_text(os.getenv("ABFSS_BASE_URI", ""), "ABFSS_BASE_URI", 2048)
    parsed = urlparse(base_uri)
    if (
        parsed.scheme.lower() != "abfss"
        or not parsed.username
        or not parsed.hostname
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError(
            "ABFSS_BASE_URI debe ser una ruta abfss válida, sin consulta ni fragmento."
        )
    return f"{base_uri.rstrip('/')}/{validate_file_name(file_name)}"


def read_abfss_file(blob_uri: str) -> bytes:
    parsed = urlparse(blob_uri)
    file_system = parsed.username
    file_path = unquote(parsed.path.lstrip("/"))
    if not file_system or not parsed.hostname or not file_path:
        raise ValidationError("La ruta ABFSS del archivo no es válida.")

    try:
        service_client = DataLakeServiceClient(
            account_url=f"https://{parsed.hostname}",
            credential=DefaultAzureCredential(exclude_interactive_browser_credential=True),
        )
        file_client = service_client.get_file_system_client(file_system).get_file_client(file_path)
        if file_client.get_file_properties().size > MAX_FILE_SIZE_BYTES:
            raise ValidationError(f"El archivo supera el límite de {MAX_FILE_SIZE_BYTES} bytes.")
        return file_client.download_file().readall()
    except AzureError as error:
        raise RuntimeError(f"No se pudo obtener el archivo de ADLS Gen2: {error}") from error


@lru_cache(maxsize=32)
def load_xsd_schema(namespace: str) -> etree.XMLSchema | None:
    xsd_directory = os.getenv("ISO20022_XSD_DIR")
    if not xsd_directory:
        return None

    directory = Path(xsd_directory)
    matching_files = [
        path for path in directory.glob("*.xsd")
        if f'targetNamespace="{namespace}"' in path.read_text(encoding="utf-8")
        or f"targetNamespace='{namespace}'" in path.read_text(encoding="utf-8")
    ]
    if not matching_files:
        raise ValidationError(f"No existe un XSD configurado para el namespace '{namespace}'.")
    try:
        schema_document = etree.parse(
            str(matching_files[0]),
            etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False),
        )
        return etree.XMLSchema(schema_document)
    except (OSError, etree.XMLSyntaxError, etree.XMLSchemaParseError) as error:
        raise ValidationError(f"No se pudo cargar el XSD de ISO 20022: {error}") from error


def count_transactions(root: ET.Element) -> int:
    primary_count = sum(
        1 for element in root.iter() if local_name(element.tag) in PRIMARY_TRANSACTION_ELEMENTS
    )
    if primary_count:
        return primary_count

    detail_count = sum(1 for element in root.iter() if local_name(element.tag) == "TxDtls")
    if detail_count:
        return detail_count
    return sum(1 for element in root.iter() if local_name(element.tag) == "Ntry")


def parse_iso20022(content: bytes) -> tuple[ET.Element, str, int]:
    if not content:
        raise ValidationError("El archivo está vacío.")
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise ValidationError(f"El archivo supera el límite de {MAX_FILE_SIZE_BYTES} bytes.")
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValidationError("El XML no puede incluir DTD ni entidades.")

    try:
        root = DefusedET.fromstring(content)
    except (ET.ParseError, ValueError) as error:
        raise ValidationError(f"XML inválido: {error}") from error

    root_name = local_name(root.tag)
    if root_name not in ISO20022_ROOTS:
        raise ValidationError(
            f"La raíz '{root_name}' no corresponde a un documento ISO 20022 soportado."
        )
    namespace = root.tag.partition("}")[0].lstrip("{")
    if root_name == "Document" and not namespace.startswith("urn:iso:std:iso:20022:tech:xsd:"):
        raise ValidationError("El elemento Document debe usar un namespace ISO 20022.")
    if root_name == "Document" and len(root) != 1:
        raise ValidationError("El elemento Document debe contener exactamente un mensaje de negocio.")

    schema = load_xsd_schema(namespace)
    if schema is not None:
        try:
            schema.assertValid(
                etree.fromstring(
                    content,
                    etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False),
                )
            )
        except (etree.XMLSyntaxError, etree.DocumentInvalid) as error:
            raise ValidationError(f"El XML no cumple el XSD ISO 20022: {error}") from error

    message = root[0] if root_name == "Document" else root
    message_type = local_name(message.tag)
    transaction_count = count_transactions(root)
    return root, message_type, transaction_count


def create_metadata(
    file_name: str,
    content: bytes,
    blob_uri: str,
    client_id: str,
    channel: str,
) -> FileMetadata:
    safe_name = validate_file_name(file_name)
    blob_uri = validate_blob_uri(blob_uri)
    client_id = validate_text(client_id, "client_id", 128)
    channel = validate_text(channel, "channel", 64)
    _, message_type, transaction_count = parse_iso20022(content)

    now = datetime.now(timezone.utc).isoformat()
    return FileMetadata(
        unique_identifier=str(uuid.uuid4()),
        file_name=safe_name,
        file_size=len(content),
        blob_uri=blob_uri,
        upload_date=now,
        status="VALIDATED",
        client_id=client_id,
        channel=channel,
        transaction_count=transaction_count,
        process_date=now,
        content_hash=hashlib.sha256(content).hexdigest(),
        message_type=message_type,
        validation_errors=[],
    )


def persist_metadata(metadata: FileMetadata) -> bool:
    warehouse_id = os.getenv("DATABRICKS_SQL_WAREHOUSE_ID")
    table_name = os.getenv("FILE_METADATA_TABLE")
    missing_settings = [
        name
        for name, value in {
            "DATABRICKS_SQL_WAREHOUSE_ID": warehouse_id,
            "FILE_METADATA_TABLE": table_name,
        }.items()
        if not value
    ]
    if missing_settings:
        raise RuntimeError(
            f"Faltan variables requeridas para persistir metadatos: {', '.join(missing_settings)}."
        )
    if not TABLE_NAME_PATTERN.fullmatch(table_name):
        raise RuntimeError("FILE_METADATA_TABLE debe ser un identificador SQL de hasta tres partes.")

    create_statement = f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
          unique_identifier STRING, file_name STRING, file_size BIGINT, blob_uri STRING,
          upload_date TIMESTAMP, status STRING, client_id STRING, channel STRING,
          transaction_count BIGINT, process_date TIMESTAMP, content_hash STRING,
          message_type STRING, validation_errors STRING
        ) USING DELTA
    """
    insert_statement = f"""
        INSERT INTO {table_name} VALUES
        (?, ?, ?, ?, CAST(? AS TIMESTAMP), ?, ?, ?, ?, CAST(? AS TIMESTAMP), ?, ?, ?)
    """
    access_token = os.getenv("DATABRICKS_TOKEN")
    if not access_token:
        raise RuntimeError("DATABRICKS_TOKEN es obligatorio para persistir metadatos.")
    connection_kwargs: dict[str, Any] = {
        "server_hostname": os.environ["DATABRICKS_HOST"].replace("https://", ""),
        "http_path": f"/sql/1.0/warehouses/{warehouse_id}",
        "access_token": access_token,
    }

    with sql.connect(**connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(create_statement)
            cursor.execute(
                insert_statement,
                (
                    metadata.unique_identifier, metadata.file_name, metadata.file_size,
                    metadata.blob_uri, metadata.upload_date, metadata.status,
                    metadata.client_id, metadata.channel, metadata.transaction_count,
                    metadata.process_date, metadata.content_hash, metadata.message_type,
                    json.dumps(metadata.validation_errors),
                ),
            )
    return True


@app.get("/")
def index() -> str:
    return render_template_string(PAGE)


@app.post("/api/files/validate")
def validate_file():
    file_name = request.form.get("file_name", "")

    try:
        blob_uri = build_blob_uri(file_name)
        metadata = create_metadata(
            file_name,
            read_abfss_file(blob_uri),
            blob_uri,
            request.form.get("client_id", ""),
            request.form.get("channel", ""),
        )
    except ValidationError as error:
        logger.info("ISO 20022 validation failed for %s: %s", file_name, error)
        return jsonify(error=str(error), status="FAILED"), 400
    except RuntimeError as error:
        logger.exception("Unable to retrieve ISO 20022 file from ADLS Gen2")
        return jsonify(error=f"No se pudo obtener el archivo de ADLS Gen2: {error}"), 503

    try:
        persisted = persist_metadata(metadata)
    except (KeyError, RuntimeError, sql.Error) as error:
        logger.exception("Unable to persist ISO 20022 metadata")
        return jsonify(error=f"Archivo válido, pero no se pudo persistir el metadato: {error}"), 503

    response = asdict(metadata)
    response["persisted"] = persisted
    return jsonify(response), 201


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
