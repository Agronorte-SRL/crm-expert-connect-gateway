# CRM Expert Connect Gateway

El gateway mantiene las credenciales de Expert Connect y OpenAI fuera del navegador.

**Base de datos (01/10/2026):** corre sólo sobre Postgres (Cloud SQL / VM de GCP). La base de MariaDB del VPS se migró al esquema `vps4_agronorte_prod`; el gateway se conecta con `DATABASE_URL` (se acepta el formato de SQLAlchemy `postgresql+psycopg2://…`, el mismo que usa Expert Connect migrado) o con `DB_POSTGRES_HOST/PORT/USER/PASSWORD/NAME`, con `search_path` = `CRM_DB_SCHEMA` (por defecto `vps4_agronorte_prod`) y sesión en UTC. Lee `parametrosaplicacion` (credenciales; la columna `"accessToken"` quedó en camelCase y se cita), `chatbot_conversations` y `chatbot_messages`, y crea al arrancar sus tablas propias, que la migración no trajo: `crm_expert_connect_messages`, `crm_expert_connect_notification_reads` y `crm_expert_connect_ticket_summaries` (idempotente; el usuario de la base necesita permiso de CREATE en el esquema). El CRM continúa consultando sus datos operativos directamente en Supabase; `bot-nosis-go` solamente autentica y correlaciona las solicitudes de esta integración.

## Tickets de cobranzas

`GET /internal/crm/expert-connect/tickets?phone=<telefono>&include_messages=<bool>` obtiene los IDs de ticket desde el índice local sincronizado de `chatbot_conversations` y consulta en Expert Connect únicamente el detalle de esas conversaciones. Esto evita recorrer el listado global del proveedor —que puede devolver cientos de resultados y agotar su límite de solicitudes— sin perder tickets abiertos ni cerrados del contacto. Cada detalle se valida contra `CRM_EXPERT_CONNECT_TEAM_ID` y `CRM_EXPERT_CONNECT_TAG_ID`; en producción corresponden a **Administración de Ventas** y **Cobranzas maquinarias**. La respuesta contiene una entidad por ticket; las notas públicas se incluyen solamente cuando se solicita el detalle.

## Respuestas dentro del ticket y mensajes pendientes (08/10/2026)

`POST /internal/crm/expert-connect/messages` acepta `ticket_id` opcional. Con él no se crea ticket: se verifica que el ticket esté indexado para ese teléfono, que sea de Cobranzas (equipo y etiqueta canónicos) y que siga abierto (si cerró responde 409), y se publica la nota pública ahí (`ticket_created=false`). Así el CRM muestra una sola card por conversación.

`GET /internal/crm/expert-connect/pending` lista los tickets abiertos de Cobranzas cuyo último mensaje es del cliente: `{pending: [{ticket_id, phone_tail, text, occurred_at}]}`. Candidatos: conversaciones del área `CRM_EXPERT_CONNECT_AREA_ID` creadas por el CRM (tienen `participant_phone`) con algún mensaje en los últimos 14 días. No se usa `chatbot_messages.direction` porque no es confiable: las respuestas del cliente dentro de un ticket del CRM quedan guardadas como salientes de agente (y `text` llega vacío). Expert Connect decide estado, equipo/etiqueta y quién escribió la última nota. Cada ticket se vuelve a consultar sólo cuando cambia su último mensaje (o a los 10 minutos), con un tope de 12 s de consultas por pedido; la respuesta se cachea 5 s.

`POST /internal/crm/expert-connect/tickets/close` (`{ticket_id, phone, tag_id?}`): verifica con el índice local que el ticket sea del teléfono y de Cobranzas, aplica el motivo sólo si viene (las etiquetas de Cobranzas no tienen subetiquetas), cierra el ticket y limpia las cachés de detalle y pendientes. El resumen con IA se genera en segundo plano, después de responder.

`GET /internal/crm/expert-connect/tickets` acepta `ticket_id`: lee sólo ese ticket del teléfono (lo usa el modal de conversación). El detalle de cada ticket se reutiliza 15 s (5 s con mensajes), porque las llamadas al proveedor van de a una con medio segundo entre ellas y la relectura de todos los tickets superaba el timeout de 25 s de bot-nosis-go.

Nota: `chatbot_conversations` no tiene el área "Administracion de Ventas" que usan las notificaciones, y `status` nunca queda en `closed`; por eso la campanita de notificaciones del CRM no muestra respuestas de Cobranzas.

## Documentos de ventas (AGRONORTE-500)

`POST /internal/crm/expert-connect/documents` lo usa `agn-miagronorte-ventas-be` para enviar el Presupuesto o la Factura proforma de una cotización. Cuerpo: `{phone, contact_name, text (1–1000), team_id, tag_name?, file_url, file_name?, external_ref}`.

1. Descarga `file_url` (el PDF que ventas-be ya publicó), exige que empiece con `%PDF-` y no pase de 32.505.856 bytes, y lo sube a `POST /api/v1/media` (multipart `file`, con el header `CompanyId`). Si el archivo falla no se crea nada en Expert Connect.
2. Busca o crea el contacto por teléfono (`externalId` = `external_ref`).
3. Crea **siempre** un ticket nuevo en `team_id` (Ventas Ag&T), **sin asignar asesor**, y marca la conversación como `advisor_handoff_ignored` para que el bot no responda.
4. Aplica la etiqueta `tag_name` buscándola por nombre en `GET /tags`, también entre las `subTags` (las de ventas son subetiquetas de VENTAS; al ticket se le aplican VENTAS y la subetiqueta) (Nuevo sin canje, Nuevo con canje, Usado sin canje, Usado con canje, Plan de Ahorro; se crean a mano). Si no existe o falla, el envío sigue y responde `tag_applied=false`.
5. Publica **una sola nota pública** con el texto y el PDF (`mediaId`). Fuera de la ventana de 24 h Expert Connect la envuelve en su plantilla ("Tienes un nuevo mensaje de AGRONORTE: … Responda para continuar la conversación."), por eso el texto llega en una sola línea e incluye también el enlace al PDF.

Respuesta: `{contact_id, contact_created, ticket_id, team_id, area_id, status, tag_id, tag_applied, note_id, media_id}`. Si falla después de crear el ticket, el error trae `detail = {message, ticket_id}` para que el llamador lo audite. El proveedor no ofrece idempotencia: cada llamada crea un ticket, así que ventas-be registra el envío como "enviando" antes de llamar y nunca reintenta solo. No hay avisos de entrega ni de lectura.

## Resúmenes

Los tickets abiertos se entregan con `summary_status=waiting_for_close`. Al cerrar desde el CRM se recuperan todas las notas públicas y se intenta generar el resumen. Los cierres externos se detectan al sincronizar el historial y se procesan como tarea de fondo, de modo que la latencia de OpenAI no bloquee el timeline.

El estado terminal del proveedor es `status=2` (también se aceptan sus representaciones textuales `closed`, `resolved` y `cerrado`). `status=1` no es terminal y no debe iniciar un resumen. Esta traducción es la que activa el backfill automático de IA para conversaciones cerradas históricas.

`crm_expert_connect_ticket_summaries` guarda el resumen, hash SHA-256 del transcript normalizado, cantidad de mensajes, modelo, versión del prompt, suficiencia de evidencia, fechas y errores. El mismo hash generado se reutiliza. El prompt explica que `chatbot_conversations` identifica la conversación y el participante, mientras `chatbot_messages.direction=in` representa al cliente y `direction=out` a Agronorte; luego distingue los salientes humanos (`advisor`) de automatizaciones (`bot`). No se envían notas privadas, borradores ni eventos técnicos. Además de instruir al modelo, el gateway valida que el resultado tenga una única oración y como máximo 45 palabras; una salida inválida queda en estado fallido y puede reintentarse.

La llamada usa `POST /v1/responses` con Structured Outputs y `store=false`. Configuración:

- `OPENAI_API_KEY`: secreto requerido para generar; si falta, el ticket queda pendiente sin afectar su cierre ni visualización.
- `OPENAI_SUMMARY_MODEL`: modelo configurable, con `gpt-4.1-mini` como valor predeterminado.
- `CRM_EXPERT_CONNECT_TEAM_ID`, `CRM_EXPERT_CONNECT_TAG_ID`: filtro canónico.

## Despliegue en la VM de GCP

Expert Connect migrado corre en `docker-server-02` (proyecto `agn-homologacion`), compose `/srv/gcp-migration/expert-connect-gcp-preview-20260929/compose.yml`, con un nginx propio (`private/nginx.conf`). Para sumar el gateway sin tocar lo existente:

1. Agregar al compose un servicio `crm-gateway` con esta imagen (puerto 28012), en la misma red que `ingest-api`, con el mismo `DATABASE_URL` que usan los servicios de Expert Connect más `CRM_EXPERT_CONNECT_GATEWAY_TOKEN`, `OPENAI_API_KEY` y las variables `CRM_*` de `docker-compose.yml`.
2. Agregar al nginx, antes de `location /`: `location ^~ /internal/crm/expert-connect/ { proxy_pass http://crm-gateway:28012; }` (con los mismos timeouts de `nginx.location.conf`).
3. Clientes: `bot-nosis-go` (CRM) y `agn-miagronorte-ventas-be` (`EXPERT_CONNECT_GATEWAY_BASE_URL` + `EXPERT_CONNECT_GATEWAY_TOKEN`).

**Desplegado el 01/10/2026** en el preview: servicio `crm-gateway` (contenedor `expert-connect-preview-crm-gateway-20260929`, imagen `agn-preview-crm-gateway:20261001`, código en `crm-gateway/`), mismo `private/preview.env` que Expert Connect (DATABASE_URL, EXPERT_CONNECT_GATEWAY_TOKEN, OPENAI_API_KEY) y ruta en `private/nginx.conf` con 120 s de lectura (el envío de documentos sube el PDF). Respaldos: `compose.before-crm-gateway-20261001T121949Z.yml` y `private/nginx.before-crm-gateway-20261001T121949Z.conf`. URL pública: `https://expert-connect-gcp.myagronorte.com.ar/internal/crm/expert-connect/…` (401 sin token).

**Corrección del 06/10/2026 (502 en el CRM):** nginx resolvía `crm-gateway` una sola vez al arrancar; cuando el contenedor del gateway se reinició y Docker le dio otra IP (192.168.64.8 → 192.168.64.11), nginx siguió mandando a la vieja y el historial, los tickets y las notificaciones del CRM dieron 502. Se recargó nginx y la ruta del CRM en `private/nginx.conf` pasó a resolver el nombre en cada consulta con el DNS interno de Docker:
`location ^~ /internal/crm/expert-connect/ { resolver 127.0.0.11 valid=10s ipv6=off; set $crm_gateway_upstream crm-gateway:28012; proxy_pass http://$crm_gateway_upstream; ... }`.
Respaldo previo: `private/nginx.conf.before-crm-resolver-20261006T195359Z`. El archivo está montado de a uno en el contenedor: editarlo sin cambiar el inodo (p. ej. `cat nuevo > nginx.conf`), no con `sed -i`, y después `nginx -t && nginx -s reload`.

## Validación

Ejecutar `python -m unittest -v test_app.py` y `python -m py_compile app.py`. `test_postgres_integration.py` prueba todas las consultas contra un Postgres descartable (instrucciones en el archivo; se saltea sin `CRM_TEST_DATABASE_URL`). Para una validación real y optativa, exportar `OPENAI_API_KEY` sólo en el proceso local y ejecutar `python live_openai_smoke.py`; reemplaza la base por un recorder en memoria y comprueba llamada real, contrato y persistencia sin tocar datos productivos.
