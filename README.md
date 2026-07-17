# Validador ISO 20022 para Databricks Apps

Aplicación Flask para cargar un archivo XML ISO 20022, validar su estructura de
forma segura y registrar metadatos en una tabla Delta mediante Databricks SQL.

## Validaciones

- El archivo debe ser XML, no estar vacío y no superar `MAX_FILE_SIZE_BYTES`
  (20 MiB por defecto).
- Se rechazan DTD y entidades XML; el análisis usa `defusedxml`.
- La raíz debe ser `Document` bajo un namespace ISO 20022, o un encabezado de
  aplicación ISO 20022. Los documentos `Document` contienen un único mensaje
  de negocio. Si se configura `ISO20022_XSD_DIR`, además se valida contra el
  XSD oficial de la versión detectada.
- Se validan y normalizan nombre de archivo, URI del blob, cliente y canal.
- Se calcula el SHA-256 del contenido y se contabilizan elementos de
  transacción ISO 20022 comunes.

Para validación integral de campos y cardinalidades, empaquete los XSD
oficiales que admite la institución en el directorio configurado. Sin esa
variable, la aplicación valida la envoltura ISO 20022 y XML seguro.

## Metadatos registrados

La respuesta y la tabla almacenan: identificador único, nombre, tamaño, URI,
fecha de carga, estado, cliente, canal, cantidad de transacciones, fecha de
proceso y hash del contenido. También se incluyen `message_type` y
`validation_errors` para trazabilidad.

El estado es `VALIDATED` solamente después de que el XML supera las
validaciones. Si falla, la API responde `FAILED` y HTTP 400.

## Configuración de Databricks

Configure estas variables de entorno/secrets en la Databricks App:

| Variable | Requerida | Descripción |
| --- | --- | --- |
| `DATABRICKS_HOST` | Sí, para persistir | URL del workspace de Databricks. |
| `DATABRICKS_SQL_WAREHOUSE_ID` | Sí, para persistir | Identificador del SQL Warehouse. |
| `FILE_METADATA_TABLE` | Sí, para persistir | Tabla Delta destino, por ejemplo `procesamiento_archivos.default.iso20022_file_metadata`. |
| `DATABRICKS_TOKEN` | Solo local | PAT de Databricks para desarrollo local. No se usa en Databricks Apps. |
| `ABFSS_BASE_URI` | Sí | Directorio de entrada en ADLS Gen2, por ejemplo `abfss://archivos@pruebasdatabricksv104.dfs.core.windows.net/inbound/`. |
| `ISO20022_XSD_DIR` | No | Directorio con los XSD oficiales ISO 20022 admitidos. |
| `MAX_FILE_SIZE_BYTES` | No | Límite de tamaño en bytes. |

La aplicación crea la tabla indicada si no existe. La identidad asociada al token
debe tener permisos `USE CATALOG`, `USE SCHEMA`, `CREATE TABLE` e `INSERT`
sobre el destino.

La identidad que ejecuta la aplicación debe tener el rol **Storage Blob Data Reader**
sobre la cuenta de almacenamiento o el contenedor de entrada. Localmente se usa la
sesión de Azure CLI mediante `DefaultAzureCredential`; en Databricks Apps se usa la
identidad de un service principal de Azure configurada mediante secretos.

## Ejecución local

```powershell
python -m pip install -r requirements.txt
$env:DATABRICKS_HOST = "https://adb-<workspace>.azuredatabricks.net"
$env:DATABRICKS_TOKEN = "<token>" # Solo desarrollo local
$env:DATABRICKS_SQL_WAREHOUSE_ID = "<warehouse-id>"
$env:FILE_METADATA_TABLE = "procesamiento_archivos.default.iso20022_file_metadata"
$env:ABFSS_BASE_URI = "abfss://archivos@pruebasdatabricksv104.dfs.core.windows.net/inbound/"
python -m flask --app app.py run
```

Abra `http://127.0.0.1:5000`, indique el nombre del XML y sus metadatos. El endpoint
`POST /api/files/validate` recibe `file_name`, `client_id` y `channel` como
`multipart/form-data`; el archivo se descarga de `ABFSS_BASE_URI`.

## Despliegue como Databricks App

La App `iso20022-validator` usa su propia identidad OAuth para Databricks SQL.
No configure `DATABRICKS_TOKEN` en la App.

1. En **Databricks Apps**, abra `iso20022-validator` y agregue el SQL Warehouse
   `ArchivosIso` con la clave `sql-warehouse` y permiso **Can use**.
2. Agregue tres recursos de tipo **Secret** con las claves `azure-tenant-id`,
   `azure-client-id` y `azure-client-secret`. Sus valores corresponden a un
   service principal de Azure que tenga **Storage Blob Data Reader** en el
   contenedor `archivos`.
3. Conceda a la identidad de la App `USE CATALOG`, `USE SCHEMA` y
   `CREATE TABLE` sobre `procesamiento_archivos.default`.
4. Sincronice y despliegue el código:

```powershell
databricks sync . "/Workspace/Users/<tu-usuario>/iso20022-validator" --profile adb-DATI
databricks apps deploy iso20022-validator `
  --source-code-path "/Workspace/Users/<tu-usuario>/iso20022-validator" `
  --profile adb-DATI
```
