import os
import json
import logging
import odoorpc
import sqlite3
from datetime import datetime
from collections import Counter
from dotenv import load_dotenv
from google import genai

# Librerías de RAG
try:
    import chromadb
except ImportError:
    chromadb = None

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class AgentState:
    def __init__(self, db_path='agent_state.db'):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute('''CREATE TABLE IF NOT EXISTS flow_state 
                            (ticket_id INTEGER PRIMARY KEY, last_message_id INTEGER, last_processed_at TIMESTAMP)''')
            conn.commit()
        finally:
            conn.close()

    def get_last_processed_id(self, ticket_id):
        conn = sqlite3.connect(self.db_path)
        try:
            res = conn.execute("SELECT last_message_id FROM flow_state WHERE ticket_id = ?", (ticket_id,)).fetchone()
            return res[0] if res else 0
        finally:
            conn.close()

    def set_last_processed_id(self, ticket_id, message_id):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("INSERT OR REPLACE INTO flow_state (ticket_id, last_message_id, last_processed_at) VALUES (?, ?, CURRENT_TIMESTAMP)", 
                         (ticket_id, message_id))
            conn.commit()
        finally:
            conn.close()

class AgenteVentasNesta:
    """
    Agente de Ventas 100% Autónomo (IA Real + Odoo + RAG + Memoria de Ciclo de Vida).
    """
    def __init__(self, ticket_id):
        self.ticket_id = ticket_id
        self.odoo = None
        self.chroma_collection = None
        self.products_schema = {}
        self.state = AgentState()
        
        # Odoo Config
        self.host = 'prod17.odoo.imprentadiagonal.com.uy'
        self.db = 'odoo17_prod'
        self.user = 'ylan.archimowicz@imprentadiagonal.com.uy'
        self.password = os.getenv('ODOO_PASSWORD', '9a50ca725dd8c0e451e8005982589dcdf3bf8fe7')
        
        # AI Config (Vertex AI Enterprise)
        self.client = genai.Client(
            enterprise=True,
            project="project-6966617c-3e1f-4ae1-91c",
            location="global"
        )
        self.model_name = 'gemini-3.5-flash'

    def conectar(self):
        logging.info("Conectando a Odoo...")
        self.odoo = odoorpc.ODOO(self.host, protocol='jsonrpc+ssl', port=443, timeout=300)
        self.odoo.login(self.db, self.user, self.password)
        
        if chromadb:
            logging.info("Conectando a ChromaDB...")
            client = chromadb.PersistentClient(path='chroma_db_clean')
            self.chroma_collection = client.get_or_create_collection("sabiduria_comercial")
            
        with open('products_schema.json', 'r', encoding='utf-8') as f:
            self.products_schema = json.load(f)

    def _get_chatter_history(self, model, res_id):
        messages = self.odoo.env['mail.message'].search_read(
            [('res_id', '=', res_id), ('model', '=', model)],
            ['id', 'body', 'author_id', 'date', 'message_type', 'subtype_id'],
            order='id asc'
        )
        
        import re
        for m in messages:
            if m['body']:
                # 1. Reemplazar <br> y <p> por saltos de línea reales
                body = m['body'].replace('<br>', '\n').replace('<br/>', '\n').replace('</p>', '\n')
                # 2. Eliminar todos los tags HTML restantes
                body = re.sub(r'<[^>]+>', '', body)
                # 3. Limpiar HTML entities
                body = body.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&gt;', '>').replace('&lt;', '<')
                # 4. Remover colas de correos (estilo Gmail/Outlook)
                body = re.split(r'El \d{1,2}.*?escribió:', body)[0]
                body = re.split(r'El \w{3}, \d{1,2} de \w{3}.*?escribió:', body)[0]
                body = re.split(r'\nDe: .*?\nEnviado:', body)[0]
                body = re.split(r'\nFrom: .*?\nSent:', body)[0]
                body = re.split(r'________________________________', body)[0]
                body = re.split(r'-- \n', body)[0]
                # 5. Limpiar espacios múltiples
                body = re.sub(r'\n{3,}', '\n\n', body).strip()
                m['body'] = body
                
        return messages

    def _get_attachments(self, model, res_id):
        attachments = self.odoo.env['ir.attachment'].search_read(
            [('res_model', '=', model), ('res_id', '=', res_id)],
            ['id', 'name', 'mimetype', 'file_size', 'description']
        )
        return attachments

    def _get_contexto_360(self, partner_id):
        if not partner_id: return {"cliente": {"name": "Desconocido"}, "vendedor": {"id": False, "nombre": "Melanie"}, "ventas_recientes": [], "leads_activos": [], "historial_conversacion_previo": []}
        
        # 1. Identificar la jerarquía (Padre e Hijos)
        fields_to_read = ['id', 'name', 'parent_id', 'vat', 'razon_social', 'l10n_latam_identification_type_id', 'type', 'street', 'mobile', 'phone', 'email', 'is_company']
        partner_data = self.odoo.env['res.partner'].read([partner_id], fields_to_read)[0]
        
        # Si es un contacto hijo, subimos al padre para tener la visión completa
        main_partner_id = partner_data['parent_id'][0] if partner_data.get('parent_id') else partner_id
        
        # Obtener todos los IDs de la familia (Padre + todos los Hijos)
        family_ids = self.odoo.env['res.partner'].search([
            '|', ('id', '=', main_partner_id), ('parent_id', '=', main_partner_id)
        ], limit=100)
        
        # Leer datos de toda la familia para buscar direcciones de entrega
        family_members = self.odoo.env['res.partner'].read(family_ids, fields_to_read)
        
        # 2. Datos consolidados de contacto (Prioridad al Padre para RUT/Razón Social)
        main_partner = next((m for m in family_members if m['id'] == main_partner_id), partner_data)
        
        # Clasificar Direcciones
        delivery_addresses = [m['street'] for m in family_members if m['type'] == 'delivery' and m['street']]
        if not delivery_addresses and main_partner.get('street'):
            delivery_addresses = [main_partner['street']]
            
        # 3. Historial de ventas de TODA la familia
        orders = self.odoo.env['sale.order'].search_read(
            [('partner_id', 'in', family_ids)], 
            ['name', 'state', 'amount_total', 'date_order', 'client_order_ref', 'user_id', 'order_line'], 
            limit=10, order='date_order desc'
        )
        
        for order in orders:
            if order.get('order_line'):
                lines = self.odoo.env['sale.order.line'].read(order['order_line'], ['name', 'product_uom_qty', 'price_unit'])
                order['detalle_productos'] = lines

        # 4. Leads activos de toda la familia
        leads = self.odoo.env['crm.lead'].search_read(
            [('partner_id', 'in', family_ids), ('type', '=', 'opportunity')], 
            ['name', 'stage_id', 'subtitle', 'id'], 
            limit=5, order='create_date desc'
        )
        
        # 5. Vendedor sugerido
        user_ids = [o['user_id'][0] for o in orders if o.get('user_id')]
        vendedor_nombre = "Melanie" 
        vendedor_id = False
        if user_ids:
            for u_id, _ in Counter(user_ids).most_common():
                u_info = self.odoo.env['res.users'].read([u_id], ['active', 'name'])[0]
                if u_info['active']:
                    vendedor_nombre = u_info['name'].split()[0]
                    vendedor_id = u_id
                    break

        # 5.B Facturas Impagas
        facturas_impagas = self.odoo.env['account.move'].search_read(
            [('partner_id', 'in', family_ids), ('move_type', '=', 'out_invoice'), ('payment_state', 'in', ['not_paid', 'partial'])],
            ['name', 'amount_total', 'amount_residual', 'currency_id', 'invoice_origin'],
            order='invoice_date asc'
        )

        # 6. Historial de conversación (50 mensajes)
        prev_messages = self.odoo.env['mail.message'].search_read(
            ['|', ('partner_ids', 'in', family_ids), '&', ('model', 'in', ['sale.order', 'crm.lead', 'helpdesk.ticket']), ('res_id', 'in', [o['id'] for o in orders] + [l['id'] for l in leads])],
            ['id', 'body', 'date', 'model', 'res_id', 'author_id'],
            limit=50, order='date desc'
        )
        
        import re
        for m in prev_messages:
            if m['body']:
                body = m['body'].replace('<br>', '\n').replace('<br/>', '\n').replace('</p>', '\n')
                body = re.sub(r'<[^>]+>', '', body)
                body = body.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&gt;', '>').replace('&lt;', '<')
                body = re.split(r'El \d{1,2}.*?escribió:', body)[0]
                body = re.split(r'El \w{3}, \d{1,2} de \w{3}.*?escribió:', body)[0]
                body = re.split(r'\nDe: .*?\nEnviado:', body)[0]
                body = re.split(r'\nFrom: .*?\nSent:', body)[0]
                body = re.split(r'________________________________', body)[0]
                body = re.split(r'-- \n', body)[0]
                body = re.sub(r'\n{3,}', '\n\n', body).strip()
                m['body'] = body
        
        return {
            "cliente_principal": {
                "id": main_partner['id'],
                "nombre": main_partner['name'],
                "razon_social": main_partner.get('razon_social'),
                "vat_rut": main_partner.get('vat') or main_partner.get('x_rut'),
                "tipo_documento": main_partner['l10n_latam_identification_type_id'][1] if main_partner.get('l10n_latam_identification_type_id') else "Desconocido",
                "calle": main_partner.get('street'),
                "email": main_partner.get('email'),
                "telefono": main_partner.get('phone') or main_partner.get('mobile')
            },
            "contacto_que_escribe": partner_data['name'],
            "vendedor": {"id": vendedor_id, "nombre": vendedor_nombre},
            "direcciones_entrega_posibles": delivery_addresses,
            "ventas_recientes": orders,
            "facturas_impagas": facturas_impagas,
            "leads_activos": leads,
            "historial_conversacion_previo": prev_messages
        }

    def _obtener_sabiduria_rag(self, terminos_busqueda):
        if not self.chroma_collection: return "No hay base de datos histórica."
        logging.info(f"Buscando en RAG con términos: {terminos_busqueda}")
        results = self.chroma_collection.query(query_texts=terminos_busqueda, n_results=5)
        return "\n---\n".join(results['documents'][0])

    def _get_attachments(self, model, res_id):
        attachments = self.odoo.env['ir.attachment'].search_read(
            [('res_model', '=', model), ('res_id', '=', res_id)],
            ['id', 'name', 'mimetype', 'file_size', 'description', 'datas']
        )
        return attachments

    def llamar_ia(self, prompt_text, json_mode=True, attachments=None):
        config = {"response_mime_type": "application/json"} if json_mode else {}
        
        parts = [prompt_text]
        if attachments:
            for att in attachments:
                if att.get('datas') and ('image' in att['mimetype'] or 'pdf' in att['mimetype']):
                    import base64
                    # Filtrar archivos demasiado grandes por performance
                    if att.get('file_size', 0) < 5000000: # 5MB limit
                        parts.append({
                            'mime_type': att['mimetype'],
                            'data': base64.b64decode(att['datas'])
                        })
        
        response = self.client.models.generate_content(model=self.model_name, contents=parts, config=config)
        
        # Log metadata if available
        try:
            usage = response.usage_metadata
            logging.info(f"📊 Tokens ENVIADOS (Input): {usage.prompt_token_count}")
            logging.info(f"📊 Tokens RECIBIDOS (Output): {usage.candidates_token_count}")
        except: pass

        if json_mode:
            return json.loads(response.text)
        return response.text

    def _crear_factura_borrador_si_confirmado(self, so_id):
        try:
            so = self.odoo.env['sale.order'].read([so_id], ['state', 'name'])[0]
            if so['state'] not in ['sale', 'done']:
                msg = f"<b>💰 AVISO FINANCIERO</b>: Se detectó pago para el presupuesto {so['name']}, pero el mismo está en borrador. <b>Favor de confirmar el Pedido de Venta</b> para poder generar la factura y conciliar."
                # Dejamos la nota en el Lead (que es donde se ve el flujo)
                lead = self.odoo.env['crm.lead'].search_read([('order_ids', 'in', [so_id])], ['id'])
                if lead: self.odoo.env['mail.message'].create({'model': 'crm.lead', 'res_id': lead[0]['id'], 'body': msg, 'message_type': 'comment', 'subtype_id': 2})
                return []
            
            invoice_ids = self.odoo.env['sale.order']._create_invoices([so_id])
            if invoice_ids:
                logging.info(f"✅ Factura borrador creada para SO {so_id}: {invoice_ids}")
                return invoice_ids
            return []
        except Exception as e:
            logging.error(f"Error creando factura borrador para SO {so_id}: {e}")
            return []

    def _adjuntar_comprobantes_a_pago(self, pay_id, attachments):
        for att in attachments:
            if 'image' in att['mimetype'] or 'pdf' in att['mimetype']:
                try:
                    self.odoo.env['ir.attachment'].create({
                        'name': att['name'],
                        'datas': att['datas'],
                        'res_model': 'account.payment',
                        'res_id': pay_id,
                        'type': 'binary'
                    })
                    logging.info(f"📎 Comprobante '{att['name']}' adjuntado al pago {pay_id}.")
                except Exception as e:
                    logging.error(f"Error adjuntando archivo al pago: {e}")

    def count_tokens(self, text):
        if not text: return 0
        try:
            return self.client.models.count_tokens(model=self.model_name, contents=text).total_tokens
        except:
            return 0

    def ejecutar(self):
        self.conectar()
        
        # 1. Obtener datos del Ticket y flujo asociado
        ticket_data = self.odoo.env['helpdesk.ticket'].read([self.ticket_id], ['name', 'description', 'partner_id', 'stage_id'])
        if not ticket_data:
            logging.warning(f"⚠️ Ticket #{self.ticket_id} no encontrado en Odoo.")
            return
        ticket = ticket_data[0]
        partner_id = ticket['partner_id'][0] if ticket['partner_id'] else False
        
        # Buscar Lead y Sale Order asociados
        lead = self.odoo.env['crm.lead'].search_read([('custom_helpdesk_support_id', '=', self.ticket_id)], ['id', 'name', 'stage_id'], limit=1)
        lead_id = lead[0]['id'] if lead else False
        
        # Buscar todas las SO asociadas para sumar el total
        so = self.odoo.env['sale.order'].search_read([('opportunity_id', '=', lead_id)], ['id', 'name', 'state', 'amount_total'], limit=100) if lead_id else []
        so_id = so[0]['id'] if so else False
        monto_total_presupuestado = sum([s.get('amount_total', 0) for s in so]) if so else 0.0

        # 2. Recopilar Chatter Completo (Ticket + Lead + SO)
        all_messages = self._get_chatter_history('helpdesk.ticket', self.ticket_id)
        all_attachments = self._get_attachments('helpdesk.ticket', self.ticket_id)
        
        if lead_id: 
            all_messages += self._get_chatter_history('crm.lead', lead_id)
            all_attachments += self._get_attachments('crm.lead', lead_id)
        for s in so:
            all_messages += self._get_chatter_history('sale.order', s['id'])
            all_attachments += self._get_attachments('sale.order', s['id'])
        
        # Ordenar por ID para mantener cronología
        all_messages.sort(key=lambda x: x['id'])
        
        # 3. Verificar si hay novedades
        last_processed_id = self.state.get_last_processed_id(self.ticket_id)
        new_messages = [m for m in all_messages if m['id'] > last_processed_id]
        
        if not new_messages:
            logging.info(f"☕ Sin novedades para Ticket #{self.ticket_id}. Saltando...")
            return

        logging.info(f"🔔 {len(new_messages)} mensajes nuevos detectados en el flujo Ticket-Lead-SO.")

        # 4. PASO A: GENERACIÓN DE BÚSQUEDA (Llamada Barata para RAG y Filtrado de JSON)
        # Pedimos a la IA que nos diga qué buscar y a qué categorías del JSON apuntar
        categorias_disponibles = list(self.products_schema.keys())
        
        prompt_busqueda = f"""
        Eres un experto en clasificación de ventas para una IMPRENTA. Analiza la consulta del cliente (HILO RECIENTE) y determina:
        1. Términos de Búsqueda: De 6 a 10 frases clave para buscar en nuestra base de datos histórica. 
           - REGLA CRÍTICA: Somos una imprenta. Está PROHIBIDO usar palabras genéricas sueltas como "papel", "impresión", "cotización", "presupuesto" o "full color", ya que no filtran nada en nuestra base.
           - SÍ puedes usar el nombre del producto exacto (ej: "individuales", "carpetas", "afiches").
           - PREFIERE usar frases compuestas (ej: "cartel sintra", "individuales 29x37", "sintra 2mm").
           - DEBES abarcar TODOS los productos diferentes que el cliente esté pidiendo.
        2. Categorías Técnicas: Revisa la lista de categorías disponibles y selecciona SOLO las que apliquen a los productos que el cliente quiere cotizar.
        
        CONSULTA: {ticket['description']}
        HILO RECIENTE: {" ".join([m['body'] for m in new_messages])}
        ARCHIVOS ADJUNTOS: {[a['name'] for a in all_attachments]}
        
        CATEGORÍAS DISPONIBLES EN EL SISTEMA: {categorias_disponibles}
        
        Responde SOLO un JSON con este formato: 
        {{
            "terminos": ["termino1", "termino2", ...],
            "categorias": ["Categoria 1", "Categoria 2"]
        }}
        """
        logging.info("Generando términos de búsqueda y filtrando categorías (Llamada Barata)...")
        # Pasamos adjuntos por si el cliente mandó el comprobante mudo
        resp_busqueda = self.llamar_ia(prompt_busqueda, attachments=all_attachments)
        terminos = resp_busqueda.get("terminos", [ticket['name']])
        categorias_elegidas = resp_busqueda.get("categorias", [])
        
        # Filtrar el JSON de Productos
        json_filtrado = {}
        for cat in categorias_elegidas:
            if cat in self.products_schema:
                json_filtrado[cat] = self.products_schema[cat]
        
        # Si la IA falló en elegir o no hay categorías, mandamos un JSON mínimo vacío para no romper la app pero no gastar tokens
        if not json_filtrado:
            json_filtrado = {"Aviso": "No se detectó una categoría específica en el JSON de reglas técnicas."}
            logging.warning("⚠️ No se detectaron categorías válidas en la consulta. JSON técnico omitido para ahorrar tokens.")
        else:
            logging.info(f"✅ JSON Filtrado por categorías: {list(json_filtrado.keys())}")
        
        # 5. Obtener Sabiduría RAG
        sabiduria_rag = self._obtener_sabiduria_rag(terminos)
        
        # 6. Recopilar Contexto 360
        ctx_360 = self._get_contexto_360(partner_id)
        
        # 7. Leer el Prompt Maestro
        with open('PROMPT_AGENTE_VENTAS.md', 'r', encoding='utf-8') as f:
            master_prompt = f.read()
            
        # 8. Construir Prompt de entrada final
        chatter_summary = "\n".join([f"[{m['date']}] {m['author_id'][1] if m['author_id'] else 'Sistema'}: {m['body']}" for m in all_messages])
        attachment_summary = "\n".join([f"- {a['name']} ({a['mimetype']}, {a['file_size']} bytes)" for a in all_attachments])
        
        input_ia = f"""
        {master_prompt}
        
        --- ESTADO ACTUAL DEL FLUJO ---
        TICKET STAGE: {ticket['stage_id'][1]}
        LEAD STAGE: {lead[0]['stage_id'][1] if lead else 'N/A'}
        SALE ORDER STATE: {so[0]['state'] if so else 'N/A'}
        MONTO TOTAL PRESUPUESTADO: ${monto_total_presupuestado}
        
        --- HILO DE CONVERSACIÓN (CHATTER) ---
        {chatter_summary}

        --- ARCHIVOS ADJUNTOS DETECTADOS ---
        {attachment_summary if all_attachments else "No hay archivos adjuntos."}

        --- CONTEXTO ADICIONAL ---
        FICHA 360 DEL CLIENTE: {json.dumps(ctx_360, ensure_ascii=False)}
        SABIDURÍA COMERCIAL (RAG): {sabiduria_rag}
        REGLAS TÉCNICAS (PRODUCTOS): {json.dumps(json_filtrado, ensure_ascii=False)}
        """
        
        # DEBUG: Guardar el input para inspección
        with open('debug_input_ia.txt', 'w', encoding='utf-8') as f:
            f.write(input_ia)
        logging.info("DEBUG: Input de la IA guardado en debug_input_ia.txt")
        
        # --- PERFILADO DE TOKENS DETALLADO ---
        t_prompt = self.count_tokens(master_prompt)
        t_chatter = self.count_tokens(chatter_summary)
        t_ficha = self.count_tokens(json.dumps(ctx_360, ensure_ascii=False))
        t_rag = self.count_tokens(sabiduria_rag)
        t_reglas = self.count_tokens(json.dumps(json_filtrado, ensure_ascii=False))
        t_adjuntos = self.count_tokens(attachment_summary if all_attachments else "No hay archivos adjuntos.")
        t_total_calc = t_prompt + t_chatter + t_ficha + t_rag + t_reglas + t_adjuntos
        
        logging.info(f"--- REPORTE DE TOKENS ENVIADOS ---")
        logging.info(f"Tokens enviados de Prompt Maestro: {t_prompt}")
        logging.info(f"Tokens enviados de Chatter: {t_chatter}")
        logging.info(f"Tokens enviados de Ficha 360: {t_ficha}")
        logging.info(f"Tokens enviados de Sabiduría RAG: {t_rag}")
        logging.info(f"Tokens enviados de Reglas Técnicas: {t_reglas}")
        logging.info(f"Tokens enviados de Adjuntos/Metadata: {t_adjuntos}")
        logging.info(f"Tokens enviados totales: {t_total_calc}")
        logging.info(f"-----------------------------------")
        
        logging.info("Llamando a la IA Real (Gemini) para el dictamen final...")
        dictamen = self.llamar_ia(input_ia)
        
        # 9. EJECUCIÓN EN ODOO BASADA EN EL DICTAMEN
        logging.info("Procesando lista de operaciones...")
        operaciones = dictamen.get('lista_operaciones', [])
        
        # El borrador y el análisis son globales al mensaje
        borrador_transicion = dictamen.get('borradores_respuesta', {}).get('helpdesk_transicion')
        borrador_crm = dictamen.get('borradores_respuesta', {}).get('crm_venta')
        analisis_global = dictamen.get('analisis_contexto', '')
        suposiciones_globales = dictamen.get('suposiciones_expertas', [])
        
        # Para evitar enviar el borrador varias veces, lo hacemos al final
        so_ids_afectados = set()
        
        for index, operacion in enumerate(operaciones):
            logging.info(f"--- Ejecutando Operación {index + 1}: {operacion.get('categoria')} ---")
            
            # A. Actualizar Clasificación del Ticket (SOLO si no hay Lead)
            if not lead_id:
                ticket_vals = {}
                if operacion.get('acciones_odoo', {}).get('clasificacion_ticket'):
                    ticket_vals['ticket_type_id'] = operacion['acciones_odoo']['clasificacion_ticket'].get('ticket_type_id')
                    ticket_vals['tag_ids'] = [(6, 0, operacion['acciones_odoo']['clasificacion_ticket'].get('tag_ids', []))]
                
                if ctx_360.get('vendedor', {}).get('id'):
                    ticket_vals['user_id'] = ctx_360['vendedor']['id']
                    
                if ticket_vals:
                    self.odoo.env['helpdesk.ticket'].write([self.ticket_id], ticket_vals)
                    logging.info(f"✅ Ticket #{self.ticket_id} clasificado inicialmente.")
            else:
                if index == 0: logging.info("⏭️ Omitiendo clasificación de Ticket (Ya existe Lead asociado).")

            # B. Actualizar Datos del Cliente (Genérico + Logística)
            partner_update = {}
            ai_updates = operacion.get('extraccion_datos', {}).get('datos_partner_actualizar', {})
            
            valid_fields = ['vat', 'razon_social', 'phone', 'street', 
                            'x_delivery_from', 'x_delivery_to', 
                            'x_day_monday', 'x_day_tuesday', 'x_day_wednesday', 
                            'x_day_thursday', 'x_day_friday']
            
            def time_to_float(t_str):
                if not t_str or ':' not in str(t_str): return None
                try:
                    parts = str(t_str).split(':')
                    return float(parts[0]) + (float(parts[1])/60.0)
                except: return None

            for field, value in ai_updates.items():
                if field in valid_fields and value is not None:
                    if field == 'vat': value = str(value)
                    if field in ['x_delivery_from', 'x_delivery_to']:
                        value = time_to_float(value)
                    if value is not None:
                        partner_update[field] = value
            
            if partner_update and partner_id:
                target_id = ctx_360['cliente_principal']['id'] if 'id' in ctx_360['cliente_principal'] else partner_id
                self.odoo.env['res.partner'].write([target_id], partner_update)
                logging.info(f"✅ Datos del cliente actualizados: {partner_update}")

            # C. Creación o Actualización del Flujo (Lead/SO)
            lead_existia = bool(lead_id)
            so_principal_name = so[0]['name'] if so else ""
            nuevo_presupuesto_separado = operacion.get('acciones_odoo', {}).get('nuevo_presupuesto_separado', False)
            so_nuevo_creado = False
            current_so_id = so_id # Usamos una variable local para la operación iterativa

            if operacion.get('acciones_odoo', {}).get('crear_oportunidad') and not lead_id:
                lead_id = self.odoo.env['crm.lead'].create({
                    'name': ticket['name'],
                    'partner_id': partner_id,
                    'type': 'opportunity',
                    'custom_helpdesk_support_id': self.ticket_id,
                    'user_id': ctx_360['vendedor']['id'] if ctx_360['vendedor']['id'] else None,
                    'stage_id': 2 # Completando datos
                })
                
                current_so_id = self.odoo.env['sale.order'].create({
                    'partner_id': partner_id,
                    'partner_invoice_id': ctx_360['cliente_principal']['id'],
                    'opportunity_id': lead_id,
                    'user_id': ctx_360['vendedor']['id'] if ctx_360['vendedor']['id'] else None
                })
                so_nuevo_creado = True
                so_id = current_so_id # Actualizamos el principal por si lo necesitan operaciones futuras
                logging.info(f"✅ Nuevo flujo creado (Lead {lead_id}, SO {current_so_id}).")
            
            elif lead_id:
                if index == 0:
                    if operacion.get('categoria') == 'Aceptación':
                        self.odoo.env['crm.lead'].write([lead_id], {'stage_id': 6})
                        logging.info(f"✅ Lead {lead_id} movido a 'Ganado sin OT' (Aceptación).")
                    else:
                        self.odoo.env['crm.lead'].write([lead_id], {'stage_id': 2})
                        logging.info(f"✅ Lead {lead_id} movido a 'Completando datos'.")
                
                if nuevo_presupuesto_separado:
                    current_so_id = self.odoo.env['sale.order'].create({
                        'partner_id': partner_id,
                        'partner_invoice_id': ctx_360['cliente_principal']['id'],
                        'opportunity_id': lead_id,
                        'user_id': ctx_360['vendedor']['id'] if ctx_360['vendedor']['id'] else None
                    })
                    so_nuevo_creado = True
                    logging.info(f"✅ Nuevo presupuesto separado creado: SO {current_so_id}.")
                elif current_so_id:
                    self.odoo.env['sale.order'].write([current_so_id], {'partner_invoice_id': ctx_360['cliente_principal']['id']})
                    logging.info(f"✅ Dirección de factura del SO {current_so_id} actualizada al Padre.")

            so_ids_afectados.add(current_so_id)

            # Actualizar Subtítulo y Referencias si se creó un SO
            if so_nuevo_creado and current_so_id:
                so_number = self.odoo.env['sale.order'].read([current_so_id], ['name'])[0]['name']
                subtitle_val = operacion.get('acciones_odoo', {}).get('campos_especificos', {}).get('subtitle', '')
                if subtitle_val:
                    formatted_subtitle = subtitle_val.replace('PXXXXX', so_number)
                    # Siempre leer el subtítulo actual para concatenar correctamente en bucles de múltiples SOs
                    current_lead_data = self.odoo.env['crm.lead'].read([lead_id], ['subtitle'])[0]
                    current_subtitle = current_lead_data.get('subtitle') or ''
                    if formatted_subtitle not in current_subtitle:
                        new_subtitle = f"{current_subtitle} | {formatted_subtitle}" if current_subtitle else formatted_subtitle
                        self.odoo.env['crm.lead'].write([lead_id], {'subtitle': new_subtitle[:200]}) # Límite de Odoo
                
                client_ref_val = operacion.get('acciones_odoo', {}).get('campos_especificos', {}).get('client_order_ref', '')
                if client_ref_val:
                    self.odoo.env['sale.order'].write([current_so_id], {'client_order_ref': client_ref_val.replace('PXXXXX', so_number).replace('{so_number}', so_number)})

            # D. Gestión de Líneas de Venta (Producto y Envío)
            if current_so_id:
                # 1. Producto Principal
                existing_lines = self.odoo.env['sale.order.line'].search_read([('order_id', '=', current_so_id), ('product_id.name', 'not ilike', 'Envío')], limit=1)
                
                extraccion = operacion.get('extraccion_datos', {})
                line_vals = {
                    'name': extraccion.get('descripcion_texto_plano', ''),
                    'product_description': extraccion.get('descripcion_html', ''),
                    'product_uom_qty': extraccion.get('cantidad') or 1,
                }
                
                if existing_lines and not nuevo_presupuesto_separado:
                    self.odoo.env['sale.order.line'].write([existing_lines[0]['id']], line_vals)
                    logging.info(f"✅ Línea de producto principal actualizada (SO {current_so_id}).")
                else:
                    search_terms = ['Señal', 'Cartel', 'Otro']
                    prod_id = False
                    for term in search_terms:
                        ids = self.odoo.env['product.product'].search([('name', 'ilike', term)], limit=1)
                        if ids: prod_id = ids[0]; break
                    
                    if prod_id:
                        line_vals.update({'order_id': current_so_id, 'product_id': prod_id, 'price_unit': 0.0})
                        self.odoo.env['sale.order.line'].create(line_vals)
                        logging.info(f"✅ Línea de producto principal creada (SO {current_so_id}).")

                    # 1.B Agregar notas opcionales como líneas separadas
                    notas_opcionales = extraccion.get('notas_opcionales', [])
                    for nota in notas_opcionales:
                        self.odoo.env['sale.order.line'].create({
                            'order_id': current_so_id,
                            'display_type': 'line_note',
                            'name': nota
                        })
                        logging.info(f"✅ Nota opcional agregada al SO {current_so_id}.")

                    # 1.C Líneas de Diseño o Validación
                    estado_diseno = extraccion.get('estado_diseno')
                    if estado_diseno == 'requiere_diseno':
                        self.odoo.env['sale.order.line'].create({
                            'order_id': current_so_id,
                            'product_id': 5409, # Diseñar productos
                            'name': extraccion.get('detalles_diseno', 'Diseño Gráfico'),
                            'product_uom_qty': 1,
                            'price_unit': 0.0
                        })
                        operacion['requiere_cotizar_diseno'] = True
                        logging.info(f"✅ Línea de 'Diseñar productos' agregada (SO {current_so_id}).")
                    elif estado_diseno == 'provisto':
                        self.odoo.env['sale.order.line'].create({
                            'order_id': current_so_id,
                            'product_id': 5408, # Validación de diseños
                            'name': 'Validación de diseños del cliente',
                            'product_uom_qty': 1,
                            'price_unit': 0.0
                        })
                        logging.info(f"✅ Línea de 'Validación de diseños' agregada (SO {current_so_id}).")

                    # 2. Línea de Envío (ID 14594)
                if extraccion.get('requiere_envio'):
                    envio_product_id = int(os.getenv('ENVIO_PRODUCT_ID', 14594))
                    shipping_line = self.odoo.env['sale.order.line'].search([('order_id', '=', current_so_id), ('product_id', '=', envio_product_id)], limit=100)
                    
                    envio_desc_texto = "Envío estándar a domicilio"
                    envio_desc_html = "<b>Envío estándar a domicilio</b>"
                    
                    calle = ai_updates.get('street') or ctx_360.get('cliente_principal', {}).get('calle')
                    
                    detalles_envio = []
                    if calle: detalles_envio.append(f"Dirección: {calle}")
                    
                    if ai_updates.get('x_delivery_from') is not None and ai_updates.get('x_delivery_to') is not None:
                        from_t = str(ai_updates['x_delivery_from'])[:5]
                        to_t = str(ai_updates['x_delivery_to'])[:5]
                        detalles_envio.append(f"Horario: {from_t} a {to_t}")
                        
                    dias_activos = []
                    dias_map = {'x_day_monday': 'Lunes', 'x_day_tuesday': 'Martes', 'x_day_wednesday': 'Miércoles', 'x_day_thursday': 'Jueves', 'x_day_friday': 'Viernes'}
                    for key, name in dias_map.items():
                        if ai_updates.get(key): dias_activos.append(name)
                    if dias_activos: detalles_envio.append(f"Días: {', '.join(dias_activos)}")

                    if nuevo_presupuesto_separado and so_principal_name:
                        detalles_envio.append(f"Nota: Sin costo porque se junta con {so_principal_name} al momento de entregar.")
                        
                    if detalles_envio:
                        envio_desc_texto += "\n" + "\n".join(detalles_envio)
                        envio_desc_html += "<br>" + "<br>".join(detalles_envio)

                    shipping_vals = {
                        'name': envio_desc_texto,
                        'product_description': envio_desc_html,
                        'product_uom_qty': 1,
                        'price_unit': 0.0
                    }

                    if not shipping_line:
                        shipping_vals.update({'order_id': current_so_id, 'product_id': envio_product_id})
                        self.odoo.env['sale.order.line'].create(shipping_vals)
                        logging.info(f"✅ Línea de envío agregada (SO {current_so_id}).")
                    else:
                        self.odoo.env['sale.order.line'].write([shipping_line[0]], shipping_vals)
                        logging.info(f"✅ Línea de envío actualizada (SO {current_so_id}).")

            # G. Registro de Pagos
            if operacion.get('categoria') == 'Pago Recibido':
                pago_data = operacion.get('extraccion_pago', {})
                if pago_data and pago_data.get('monto_pagado'):
                    monto = float(pago_data.get('monto_pagado', 0.0))
                    if monto > 0:
                        # Mapear banco a journal_id
                        banco_str = str(pago_data.get('banco_detectado', '')).lower()
                        moneda = pago_data.get('moneda', 'UYU')
                        journal_id = 9 # Default BROU $
                        if 'brou' in banco_str and moneda == 'USD': journal_id = 10
                        elif 'mercado' in banco_str or 'mp' in banco_str: journal_id = 18
                        elif 'master' in banco_str: journal_id = 23 if moneda == 'UYU' else 25
                        elif 'visa' in banco_str: journal_id = 24 if moneda == 'UYU' else 26
                        
                        target_partner = ctx_360['cliente_principal']['id'] if 'id' in ctx_360['cliente_principal'] else partner_id
                        
                        # 1. Facturar SO si la IA lo indica
                        target_invoice_ids = []
                        so_to_invoice = pago_data.get('facturar_y_cobrar_so')
                        if so_to_invoice:
                            match_so = self.odoo.env['sale.order'].search([('name', '=', so_to_invoice)], limit=100)
                            if match_so:
                                target_invoice_ids = self._confirmar_y_facturar_so(match_so[0])

                        # 2. Buscar IDs de facturas existentes mencionadas por la IA
                        facturas_mencionadas = pago_data.get('facturas_asociadas', [])
                        if facturas_mencionadas:
                            found_invs = self.odoo.env['account.move'].search([('name', 'in', facturas_mencionadas), ('move_type', '=', 'out_invoice')], limit=100)
                            target_invoice_ids.extend(found_invs)

                        # 3. Definir la Referencia
                        es_sena = pago_data.get('es_sena_o_saldo') == 'seña'
                        ref_str = so_principal_name if es_sena else ", ".join(facturas_mencionadas)
                        
                        payment_vals = {
                            'partner_id': target_partner,
                            'amount': monto,
                            'journal_id': journal_id,
                            'payment_type': 'inbound',
                            'partner_type': 'customer',
                            'ref': ref_str[:100]
                        }
                        
                        try:
                            pay_id = self.odoo.env['account.payment'].create(payment_vals)
                            self.odoo.env['account.payment'].action_post([pay_id])
                            logging.info(f"✅ Pago {pay_id} creado y publicado ({monto} {moneda})")
                            
                            # 4. CONCILIACIÓN NATIVA
                            payment_data = self.odoo.env['account.payment'].read([pay_id], ['move_id'])[0]
                            payment_move_id = payment_data['move_id'][0]
                            
                            # Buscamos la línea 'receivable' del pago para conciliar
                            receivable_line = self.odoo.env['account.move.line'].search([
                                ('move_id', '=', payment_move_id), 
                                ('account_id.account_type', '=', 'asset_receivable'),
                                ('reconciled', '=', False)
                            ], limit=100)

                            if es_sena and current_so_id:
                                # Vínculo por módulo extra
                                self.odoo.env['sale.order'].write([current_so_id], {'adv_payment_ids': [(4, pay_id)]})
                                logging.info(f"🔗 Pago vinculado al SO {current_so_id} (Seña).")
                                msg = f"<b>💰 SEÑA REGISTRADA</b>: Pago por ${monto} vinculado al SO {so_principal_name}."
                                self.odoo.env['mail.message'].create({'model': 'crm.lead', 'res_id': lead_id, 'body': msg, 'message_type': 'comment', 'subtype_id': 2})
                            
                            elif target_invoice_ids and receivable_line:
                                # Conciliación nativa contra facturas
                                line_id = receivable_line[0]
                                for inv_id in target_invoice_ids:
                                    self.odoo.env['account.move'].js_assign_outstanding_line([inv_id], line_id)
                                    logging.info(f"🔗 Pago conciliado nativamente con Factura {inv_id}.")
                                
                                msg = f"<b>💰 SALDO CONCILIADO</b>: Pago por ${monto} asociado a facturas: {', '.join(facturas_mencionadas)}."
                                self.odoo.env['mail.message'].create({'model': 'crm.lead', 'res_id': lead_id, 'body': msg, 'message_type': 'comment', 'subtype_id': 2})

                        except Exception as e:
                            logging.error(f"Error procesando el flujo de pago: {e}")

            # H. Cancelación de Presupuestos
            if operacion.get('categoria') == 'Cancelación de Presupuesto':
                # Buscamos si la IA identificó un presupuesto específico a cancelar
                so_to_cancel = operacion.get('extraccion_datos', {}).get('otros_detalles', '')
                # Intentamos extraer un número de presupuesto tipo P79XXX
                import re
                match = re.search(r'P\d{5}', so_to_cancel)
                if match:
                    so_name = match.group()
                    target_so = self.odoo.env['sale.order'].search([('name', '=', so_name)], limit=100)
                    if target_so:
                        self.odoo.env['sale.order'].action_cancel(target_so)
                        logging.info(f"🚫 Presupuesto {so_name} cancelado automáticamente.")
                    else:
                        logging.warning(f"⚠️ Se pidió cancelar {so_name} pero no se encontró en Odoo.")
                else:
                    logging.warning("⚠️ Se pidió una cancelación pero no se detectó un número de presupuesto claro.")

        # E. Registro de Borradores y Notas Internas (Globales)
        if lead_id:
            if not lead_existia and borrador_transicion:
                self.odoo.env['mail.message'].create({
                    'model': 'helpdesk.ticket', 'res_id': self.ticket_id, 
                    'body': f"<b>[BORRADOR AI]</b><br>{borrador_transicion}", 
                    'message_type': 'comment', 'subtype_id': 2
                })

            if borrador_crm:
                self.odoo.env['mail.message'].create({
                    'model': 'crm.lead', 'res_id': lead_id, 
                    'body': f"<b>[BORRADOR AI - LISTO PARA ENVIAR]</b><br><br>{borrador_crm}", 
                    'message_type': 'comment', 'subtype_id': 2
                })

            # Nota de actualización técnica
            if any(op.get('categoria') == 'Actualización de Presupuesto Existente' for op in operaciones):
                warning_msg = """
                <div style="background-color: #fff3cd; padding: 10px; border: 1px solid #ffeeba; border-radius: 5px;">
                    <b>⚠️ ALERTA DE ACTUALIZACIÓN TÉCNICA</b><br>
                    Se han modificado las especificaciones del pedido (ej: materiales, accesorios, envío). 
                    <b>Si ya estaba presupuestado, SE DEBE VOLVER A PRESUPUESTAR para reflejar los nuevos costos.</b>
                </div>
                """
                self.odoo.env['mail.message'].create({'model': 'crm.lead', 'res_id': lead_id, 'body': warning_msg, 'message_type': 'comment', 'subtype_id': 2})
                for s_id in so_ids_afectados:
                    if s_id: self.odoo.env['mail.message'].create({'model': 'sale.order', 'res_id': s_id, 'body': warning_msg, 'message_type': 'comment', 'subtype_id': 2})

            # Nota de diseño a cotizar y rollback de etapa
            if any(op.get('requiere_cotizar_diseno') for op in operaciones):
                diseno_warning = """
                <div style="background-color: #f8d7da; padding: 10px; border: 1px solid #f5c6cb; border-radius: 5px;">
                    <b>🎨 ALERTA: DISEÑO GRÁFICO A COTIZAR</b><br>
                    El cliente solicitó servicio de diseño. Se agregó la línea al presupuesto, pero <b>SE DEBE COTIZAR EL COSTO DEL DISEÑO</b> antes de enviar o confirmar el pedido.
                </div>
                """
                self.odoo.env['mail.message'].create({'model': 'crm.lead', 'res_id': lead_id, 'body': diseno_warning, 'message_type': 'comment', 'subtype_id': 2})
                for s_id in so_ids_afectados:
                    if s_id: self.odoo.env['mail.message'].create({'model': 'sale.order', 'res_id': s_id, 'body': diseno_warning, 'message_type': 'comment', 'subtype_id': 2})
                
                # Rollback si estaba en Ganado o Esperando respuesta
                current_stage = self.odoo.env['crm.lead'].read([lead_id], ['stage_id'])[0]['stage_id'][0]
                if current_stage > 3: # 1:Nuevo, 2:Completando, 3:Presupuestando
                    self.odoo.env['crm.lead'].write([lead_id], {'stage_id': 2})
                    logging.info(f"⏪ Lead {lead_id} retrocedido a 'Completando datos' para cotizar diseño.")

            # Resumen de análisis global
            resumen_interno = f"<b>[AI - ANÁLISIS]</b> {analisis_global}<br><br><b>[AI - SUPOSICIONES]</b><br><ul>"
            for s in suposiciones_globales: resumen_interno += f"<li>{s}</li>"
            resumen_interno += "</ul>"
            self.odoo.env['mail.message'].create({'model': 'crm.lead', 'res_id': lead_id, 'body': resumen_interno, 'message_type': 'comment', 'subtype_id': 2})

        # Actualizar Memoria
        max_id = max([m['id'] for m in all_messages])
        self.state.set_last_processed_id(self.ticket_id, max_id)
        
        logging.info(f"🚀 Ciclo completado para Ticket #{self.ticket_id}. Memoria actualizada a ID {max_id}.")

if __name__ == "__main__":
    agente = AgenteVentasNesta(ticket_id=74276)
    agente.ejecutar()
