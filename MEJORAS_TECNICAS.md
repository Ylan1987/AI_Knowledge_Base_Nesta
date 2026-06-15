# Registro de Mejoras Técnicas y Refactorización

Este documento sirve como registro para planificar, ejecutar y hacer seguimiento de las mejoras técnicas y arquitectónicas identificadas durante la auditoría del código en producción.

## 🔴 1. Optimización de Consultas a Odoo (Problema N+1)
* [x] **Agente de Compras (Línea 589):** Corregir la suma dentro de la _list comprehension_ que llama a `read()` repetitivamente y con lógica defectuosa al calcular totales de facturas (`other_bills_ids`).
* [x] **Dispatcher:** Agrupar la búsqueda de mensajes de Helpdesk (`search`) en lugar de hacer un `for t_id in ticket_ids` y consultar la API por cada ticket individual.
* [x] **Creación de Adjuntos en Lote:** Reemplazar las _list comprehensions_ que hacen múltiples llamadas a `.create()` para `ir.attachment` por un único `.create()` pasándole la lista entera de diccionarios.
* [x] **Historial de Proveedor (`_get_vendor_context`):** Refactorizar el bucle que consultaba las líneas de cada factura individualmente (`account.move.line`). Ahora recopila todos los IDs de líneas de las últimas 20 facturas y hace una única llamada en bloque a Odoo, reduciendo las llamadas de red de 21 a solo 2.

## 🔴 2. Manejo Seguro de Excepciones y Logs
* [x] **Eliminación de `except: pass`:** Se eliminaron los silencios peligrosos. Los errores recuperables ahora se guardan en un arreglo de advertencias y se imprimen como "Observaciones del Bot" directamente en el Chatter de Odoo para que el usuario humano los audite sin interrumpir el flujo.
* [x] **Trazabilidad de Errores:** Cambiar `logging.error(e)` por `logging.exception("Mensaje")` en los bloques `except Exception as e:` clave, para que Python imprima el _traceback_ completo y la línea exacta del error.

## 🟡 3. Gestión Segura de Base de Datos Local (SQLite)
* [x] **Context Managers en Zoho Fetcher:** Refactorizar las aperturas de conexión (`conn = sqlite3.connect(...)`) para usar la sintaxis `with sqlite3.connect(...) as conn:` asegurando que la DB no quede bloqueada (`database is locked`) si ocurre una excepción en el medio del proceso.
* [x] **Context Managers en Dispatcher y Agentes:** Replicar la misma lógica de cierre seguro en `dispatcher.py` y `agente_compras.py`.

## 🟡 4. Eliminación de Datos "Hardcodeados" (Código Frágil)
* [x] **RUT de la Empresa:** Mover el RUT '213382910014' de `agente_compras.py` (Línea 717) al archivo `.env` o a las variables de entorno.
* [x] **IDs de Productos:** Parametrizar el `product_id = 14594` (Envío estándar) en `agente_ventas.py` para que no rompa si el ID cambia en Odoo.

## 🟡 5. Optimización de Notificaciones ATC (Spam)
* [x] **Refactorizar `_crear_actividad_atc`:** Modificar la lógica para no crear una actividad individual por cada empleado en los departamentos de Administración o ATC. Definir si se asigna a un líder de equipo, a un rol genérico, o si solo se menciona en el _chatter_ sin generar la actividad en el tablero.

## 🟢 Fase 2: Seguridad y Robustez Avanzada
* [x] **Limpieza de Credenciales:** Mover contraseñas de Odoo y UCFE del código fuente al archivo `.env`.
* [x] **Optimización de Menciones ATC:** Eliminar el N+1 en la función `_get_atc_mentions`.
* [x] **Límites en Búsquedas Odoo:** Agregar `limit=100` (o similar) a todas las consultas `.search()` que hoy no tienen límite para prevenir problemas de memoria a futuro.
* [x] **Blindaje de Parseo IA:** Envolver `json.loads()` en la función `llamar_ia` con un bloque try/except para manejar retornos de texto inesperados de la IA.

---
*Documento actualizado tras segunda auditoría integral.*
