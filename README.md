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
debe tener permisos `USE CATALOG`, `USE SCHEMA` y `CREATE TABLE` sobre el
esquema, y `SELECT` y `MODIFY` sobre la tabla destino.

Localmente, la aplicación obtiene el archivo ABFSS mediante la sesión de Azure CLI
con `DefaultAzureCredential`. En Databricks Apps, los archivos se leen desde un
volumen de Unity Catalog asociado a la external location, sin credenciales Azure
ni secretos en la App.

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
2. Agregue el volumen `procesamiento_archivos.default.iso20022_inbound` con la
   clave `input-volume` y permiso **Can read**.
3. Conceda a la identidad de la App `USE CATALOG`, `USE SCHEMA`,
   `CREATE TABLE`, `SELECT`, `MODIFY` y `READ VOLUME` sobre
   `procesamiento_archivos.default`.
4. Sincronice y despliegue el código:

```powershell
databricks sync . "/Workspace/Users/<tu-usuario>/iso20022-validator" --profile adb-DATI
databricks apps deploy iso20022-validator `
  --source-code-path "/Workspace/Users/<tu-usuario>/iso20022-validator" `
  --profile adb-DATI
```

### Configuración reproducible con Databricks CLI

Los siguientes comandos replican la configuración de recursos y permisos desde
PowerShell. Reemplace los valores entre `<...>` para el ambiente destino. El
perfil debe apuntar al workspace de ese ambiente.

```powershell
$profile = "<perfil-databricks>"
$appName = "iso20022-validator"
$warehouseName = "ArchivosIso"
$catalog = "procesamiento_archivos"
$schema = "default"
$table = "$catalog.$schema.iso20022_file_metadata"
$volume = "$catalog.$schema.iso20022_inbound"

# Identificar el ID del warehouse y la identidad de servicio de la App.
$warehouse = databricks warehouses list --output json --profile $profile |
  ConvertFrom-Json | Where-Object name -eq $warehouseName
if (-not $warehouse) { throw "No se encontró el SQL Warehouse '$warehouseName'." }

$app = databricks apps get $appName --output json --profile $profile |
  ConvertFrom-Json
$appServicePrincipal = $app.service_principal_client_id
if (-not $appServicePrincipal) { throw "No se encontró la identidad de servicio de la App." }

# Asociar el warehouse con la clave que app.yaml referencia mediante valueFrom.
# Se preservan los recursos y la configuración de despliegue ya definidos.
$resources = @($app.resources | Where-Object name -ne "sql-warehouse") + @(
  @{
    name = "sql-warehouse"
    sql_warehouse = @{
      id = $warehouse.id
      permission = "CAN_USE"
    }
  }
)
$appUpdate = @{
  resources = $resources
}
if ($app.description) { $appUpdate.description = $app.description }
if ($app.default_source_code_path) {
  $appUpdate.default_source_code_path = $app.default_source_code_path
}
if ($app.git_repository) {
  $appUpdate.git_repository = @{
    provider = $app.git_repository.provider
    url = $app.git_repository.url
  }
}
$appUpdate = $appUpdate | ConvertTo-Json -Depth 5 -Compress
databricks apps update $appName --json $appUpdate --profile $profile

# Permitir que la App cree la tabla y use el volumen.
databricks grants update catalog $catalog --json "{`"changes`":[{`"principal`":`"$appServicePrincipal`",`"add`":[`"USE_CATALOG`"]}]}" --profile $profile
databricks grants update schema "$catalog.$schema" --json "{`"changes`":[{`"principal`":`"$appServicePrincipal`",`"add`":[`"USE_SCHEMA`",`"CREATE_TABLE`"]}]}" --profile $profile
databricks grants update volume $volume --json "{`"changes`":[{`"principal`":`"$appServicePrincipal`",`"add`":[`"READ_VOLUME`"]}]}" --profile $profile

# Si la tabla ya existe, permitir la comprobación y la inserción de metadatos.
databricks grants update table $table --json "{`"changes`":[{`"principal`":`"$appServicePrincipal`",`"add`":[`"SELECT`",`"MODIFY`"]}]}" --profile $profile

# Crear un nuevo despliegue para que el runtime reciba DATABRICKS_SQL_WAREHOUSE_ID.
databricks apps deploy $appName `
  --source-code-path "/Workspace/Users/<tu-usuario>/$appName" `
  --profile $profile
```

Verifique los recursos y permisos aplicados:

```powershell
databricks apps get $appName --output json --profile $profile
databricks grants get table $table --max-results 0 --output json --profile $profile
```
