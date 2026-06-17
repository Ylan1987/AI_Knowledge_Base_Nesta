import os
import logging
import odoorpc
import sqlite3
import json
from google import genai
from datetime import datetime
from dotenv import load_dotenv
from agente_ventas import AgenteVentasNesta, AgentState
from agente_compras import AgenteComprasNesta
from cron_seguimiento import CronSeguimiento

load_dotenv()

# Parche de Seguridad Docker (QNAP) - Validado 09/06
if not os.getenv('GOOGLE_APPLICATION_CREDENTIALS'):
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '/app/gcloud_credentials.json'

os.environ['GOOGLE_CLOUD_PROJECT'] = 'project-6966617c-3e1f-4ae1-91c'

from logging.handlers import RotatingFileHandler

# ==========================================
# 🛠️ CONFIGURACIÓN DE LOGGING PARA QNAP NAS
# ==========================================
# Formato detallado: Fecha, Nivel, Archivo, Línea y Mensaje
log_formatter = logging.Formatter('%(asctime)s - [%(levelname)s] - %(filename)s:%(lineno)d - %(message)s')
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO) # Cambiar a logging.DEBUG para ver absolutamente todo

# Limpiamos handlers previos por si acaso
if root_logger.hasHandlers():
    root_logger.handlers.clear()

# 1. Handler para Consola (Útil para ver logs en la pestaña 'Console' de Container Station)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)

# 2. Handler para Archivo Físico con Rotación (Evita que el disco del NAS se llene)
# Guarda hasta 5 archivos de 5MB cada uno (25MB total de historial)
file_handler = RotatingFileHandler('nesta_dispatcher.log', maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(log_formatter)
root_logger.addHandler(file_handler)
# ==========================================

class DispatcherNesta:
    """
    Centro de Control Orquestador: Escanea novedades en Odoo y reparte el trabajo
    de forma estrictamente secuencial y modularizada.
    """
    def __init__(self):
        # ==========================================
        # 🚩 BANDERAS DE CONTROL (FEATURE FLAGS)
        # ==========================================
        self.RUN_UCFE = True
        self.RUN_ZOHO = True
        self.RUN_TICKETS_COMPRAS = True
        self.RUN_TICKETS_VENTAS = False
        self.RUN_TICKETS_OTROS = False
        self.RUN_CRM = False
        self.RUN_RESPUESTAS = True
        self.RUN_PROACTIVIDAD = False
        # ==========================================

        self.host = 'prod17.odoo.imprentadiagonal.com.uy'
        self.db = 'odoo17_prod'
        self.user = 'ylan.archimowicz@imprentadiagonal.com.uy'
        self.password = os.getenv('ODOO_PASSWORD', '9a50ca725dd8c0e451e8005982589dcdf3bf8fe7')
        self.odoo = None
        self.state = AgentState()
        self._init_dispatcher_db()
        
        # AI Config (Vertex AI Enterprise)
        self.client = genai.Client(
            vertexai=True,
            project="project-6966617c-3e1f-4ae1-91c",
            location="global"
        )
        self.model_name = 'gemini-3.5-flash'

    def _init_dispatcher_db(self):
        """Inicializa la tabla local para el tracking de leads del CRM (independiente de los tickets)."""
        conn = sqlite3.connect('agent_state.db')
        conn.execute('''CREATE TABLE IF NOT EXISTS dispatcher_lead_state 
                        (lead_id INTEGER PRIMARY KEY, last_message_id INTEGER, last_processed_at TIMESTAMP)''')
        conn.commit()
        conn.close()

    def get_last_processed_lead_id(self, lead_id):
        conn = sqlite3.connect('agent_state.db')
        res = conn.execute("SELECT last_message_id FROM dispatcher_lead_state WHERE lead_id = ?", (lead_id,)).fetchone()
        conn.close()
        return res[0] if res else 0

    def set_last_processed_lead_id(self, lead_id, message_id):
        conn = sqlite3.connect('agent_state.db')
        conn.execute("INSERT OR REPLACE INTO dispatcher_lead_state (lead_id, last_message_id, last_processed_at) VALUES (?, ?, CURRENT_TIMESTAMP)", 
                     (lead_id, message_id))
        conn.commit()
        conn.close()

    def conectar(self):
        if not self.odoo:
            self.odoo = odoorpc.ODOO(self.host, protocol='jsonrpc+ssl', port=443, timeout=120)
            self.odoo.login(self.db, self.user, self.password)

    def _call_ia_with_retry(self, prompt, retries=3):
        import time
        for i in range(retries):
            try:
                response = self.client.models.generate_content(model=self.model_name, contents=prompt)
                return response.text.strip().upper()
            except Exception as e:
                if "503" in str(e) and i < retries - 1:
                    logging.warning(f"🕒 IA sobrecargada (503), reintentando en {2**(i+1)}s...")
                    time.sleep(2**(i+1))
                    continue
                raise e

    def _notificar_descarte(self, model, res_id, bandera):
        """Escribe un log en Odoo avisando que el módulo fue desactivado y salta el procesamiento."""
        msg = f"<b>🤖 [ORQUESTADOR NESTA]</b><br>Se detectó actividad que requiere el módulo de <b>{bandera.split('_')[-1]}</b>.<br><br>⚠️ <b>ACCIÓN ABORTADA:</b> La bandera de control interna <code>{bandera}</code> se encuentra en 'False' (Apagada). Se omite la automatización."
        try:
            self.odoo.env['mail.message'].create({'model': model, 'res_id': res_id, 'body': msg, 'message_type': 'comment', 'subtype_id': 2})
        except Exception as e:
            logging.exception("Error escribiendo descarte en Helpdesk/CRM:")

    def ejecutar_ciclo_completo(self):
        self.conectar()
        
        logging.info("🚀 INICIANDO CICLO DEL ORQUESTADOR NESTA...")

        # --------------------------------------------------------------------------------
        # 1. UCFE (Sincronización DGI)
        # --------------------------------------------------------------------------------
        if self.RUN_UCFE:
            logging.info("🔎 [1/6] Ejecutando sincronización de UCFE...")
            try:
                from ucfe_connector import UCFESyncBot
                bot = UCFESyncBot()
                bot.sincronizar_todo(dias=7)
            except Exception as e:
                logging.exception("Error en UCFE:")

        # --------------------------------------------------------------------------------
        # 2. ZOHO (Casilla de Compras)
        # --------------------------------------------------------------------------------
        if self.RUN_ZOHO:
            logging.info("🔎 [2/6] Escaneando casilla de Zoho (IMAP)...")
            try:
                from zoho_fetcher import ZohoFetcher
                fetcher = ZohoFetcher()
                if fetcher.conectar():
                    emails_nuevos = fetcher.obtener_correos_nuevos()
                    for email_data in emails_nuevos:
                        logging.info(f"📧 Nuevo correo de: {email_data['from']}")
                        AgenteComprasNesta().procesar_desde_zoho(email_data)
            except Exception as e:
                logging.error(f"❌ Error en Zoho: {e}")

        # --------------------------------------------------------------------------------
        # 3. TICKETS (Entrantes - SOLO DESCUBRIMIENTO DE NUEVOS)
        # --------------------------------------------------------------------------------
        # Si las banderas de descubrimiento están prendidas, buscamos tickets en etapas iniciales
        # que el bot NUNCA haya visto (last_id == 0).
        if self.RUN_TICKETS_COMPRAS or self.RUN_TICKETS_VENTAS or self.RUN_TICKETS_OTROS:
            logging.info("🔎 [3/6] Escaneando Tickets de Helpdesk (Descubrimiento)...")
            try:
                # Buscamos solo tickets que NO estén cerrados
                ticket_ids = self.odoo.env['helpdesk.ticket'].search([('close_date', '=', False)], limit=100)
                
                # N+1 Optimization: Fetch all messages in one go
                msg_dict = {}
                if ticket_ids:
                    all_messages = self.odoo.env['mail.message'].search_read([('res_id', 'in', ticket_ids), ('model', '=', 'helpdesk.ticket')], ['id', 'res_id'], limit=100)
                    for m in all_messages:
                        msg_dict.setdefault(m['res_id'], []).append(m['id'])

                for t_id in ticket_ids:
                    last_id = self.state.get_last_processed_id(t_id)
                    
                    # SOLO procesamos si es la PRIMERA vez que lo vemos (last_id == 0)
                    if last_id == 0:
                        ticket_data = self.odoo.env['helpdesk.ticket'].read([t_id], ['name', 'description'])
                        if not ticket_data: continue
                        ticket = ticket_data[0]
                        
                        logging.info(f"🆕 Nuevo Ticket detectado: #{t_id} - {ticket['name']}")
                        prompt_class = f"""Eres un Gerente Administrativo Senior de Nesta LTDA. Tu misión es clasificar este ticket basado en el flujo de valor:
                        Título: {ticket['name']}
                        Descripción: {ticket['description']}
                        
                        CRITERIOS DE RAZONAMIENTO:
                        - COMPRA: Si el ticket involucra un proveedor externo. Esto incluye: facturas, recibos de pago, remitos de entrada de mercadería, resguardos de retención o comunicaciones de deuda.
                        - VENTA: Si el ticket involucra a un cliente de Nesta. Esto incluye: presupuestos (ej. C00590), órdenes de trabajo, envío de archivos para imprimir, reclamos de clientes o avisos de transferencias recibidas.
                        - OTRO: Solo si es una comunicación interna, spam o algo totalmente ajeno a proveedores o clientes.
                        
                        Responde ÚNICAMENTE con una palabra: VENTA, COMPRA o OTRO."""
                        categoria = self._call_ia_with_retry(prompt_class)
                        logging.info(f"🧠 Clasificación IA para Ticket #{t_id}: {categoria}")
                        
                        processed = False
                        if "COMPRA" in categoria:
                            if self.RUN_TICKETS_COMPRAS:
                                logging.info(f"🧾 Ticket #{t_id} -> Agente de Compras")
                                AgenteComprasNesta().procesar_factura(ticket_id=t_id)
                                processed = True
                            else:
                                self._notificar_descarte('helpdesk.ticket', t_id, 'RUN_TICKETS_COMPRAS')
                        elif "VENTA" in categoria:
                            if self.RUN_TICKETS_VENTAS:
                                logging.info(f"💰 Ticket #{t_id} -> Agente de Ventas")
                                AgenteVentasNesta(ticket_id=t_id).ejecutar()
                                processed = True
                            else:
                                self._notificar_descarte('helpdesk.ticket', t_id, 'RUN_TICKETS_VENTAS')
                        else:
                            if self.RUN_TICKETS_OTROS:
                                logging.info(f"❓ Ticket #{t_id} -> Etiquetado como OTRO")
                                processed = True
                            else:
                                self._notificar_descarte('helpdesk.ticket', t_id, 'RUN_TICKETS_OTROS')

                        # Si se procesó o se descartó, lo marcamos en la DB para que pase al módulo de SEGUIMIENTO
                        # Obtenemos el max ID actual de mensajes para clavar la bandera
                        messages = self.odoo.env['mail.message'].search([('res_id', '=', t_id), ('model', '=', 'helpdesk.ticket')], limit=100)
                        self.state.set_last_processed_id(t_id, max(messages) if messages else 1)

            except Exception as e:
                logging.exception("Error en Descubrimiento de Tickets:")

        # --------------------------------------------------------------------------------
        # 4. CRM (Columna "Nuevos" - SOLO DESCUBRIMIENTO)
        # --------------------------------------------------------------------------------
        if self.RUN_CRM:
            logging.info("🔎 [4/6] Escaneando CRM (Oportunidades Nuevas)...")
            try:
                leads_nuevos = self.odoo.env['crm.lead'].search([('active', '=', True), ('stage_id', '=', 1)], limit=100)
                
                # N+1 Optimization: Fetch all messages for new leads
                msg_dict_leads = {}
                if leads_nuevos:
                    all_messages = self.odoo.env['mail.message'].search_read([('res_id', 'in', leads_nuevos), ('model', '=', 'crm.lead')], ['id', 'res_id'], limit=100)
                    for m in all_messages:
                        msg_dict_leads.setdefault(m['res_id'], []).append(m['id'])

                for l_id in leads_nuevos:
                    last_msg = self.get_last_processed_lead_id(l_id)
                    if last_msg == 0:
                        logging.info(f"🆕 Nuevo Lead CRM detectado: #{l_id}")
                        lead_data = self.odoo.env['crm.lead'].read([l_id], ['custom_helpdesk_support_id'])[0]
                        if lead_data.get('custom_helpdesk_support_id'):
                            t_id = lead_data['custom_helpdesk_support_id'][0]
                            # El descubrimiento de CRM depende de que la lógica de VENTAS esté activa
                            # pero el trigger es el Lead nuevo.
                            logging.info(f"➡️ Procesando Lead #{l_id} vía Agente de Ventas")
                            AgenteVentasNesta(ticket_id=t_id).ejecutar()
                        
                        # Marcamos como procesado para seguimiento
                        messages = msg_dict_leads.get(l_id, [])
                        self.set_last_processed_lead_id(l_id, max(messages) if messages else 1)
            except Exception as e:
                logging.exception("Error en Descubrimiento de CRM:")

        # --------------------------------------------------------------------------------
        # 5. RESPUESTAS (Seguimiento de interacción - BASADO 100% EN DB LOCAL)
        # --------------------------------------------------------------------------------
        if self.RUN_RESPUESTAS:
            logging.info("🔎 [5/6] Escaneando respuestas en Tickets y CRM (Seguimiento DB)...")
            try:
                conn = sqlite3.connect('agent_state.db')
                
                # A. SEGUIMIENTO DE TICKETS
                active_tickets = conn.execute("SELECT ticket_id, last_message_id FROM flow_state").fetchall()
                
                # N+1 Optimization
                ticket_ids_followup = [t[0] for t in active_tickets]
                msg_dict_followup = {}
                if ticket_ids_followup:
                    all_messages = self.odoo.env['mail.message'].search_read([('res_id', 'in', ticket_ids_followup), ('model', '=', 'helpdesk.ticket')], ['id', 'res_id'], limit=100)
                    for m in all_messages:
                        msg_dict_followup.setdefault(m['res_id'], []).append(m['id'])

                for t_id, last_msg_id in active_tickets:
                    messages = [m_id for m_id in msg_dict_followup.get(t_id, []) if m_id > last_msg_id]
                    if messages:
                        logging.info(f"💬 Nueva interacción en Ticket activo #{t_id}")
                        
                        # VALIDACIÓN DE BANDERAS ANTES DE PROCESAR
                        ticket_data = self.odoo.env['helpdesk.ticket'].read([t_id], ['name', 'description'])[0]
                        prompt_class = f"""Eres un Gerente Administrativo Senior de Nesta LTDA. Tu misión es clasificar este ticket basado en el flujo de valor:
                        Título: {ticket_data['name']}
                        Descripción: {ticket_data['description']}
                        
                        CRITERIOS DE RAZONAMIENTO:
                        - COMPRA: Si el ticket involucra un proveedor externo. Esto incluye: facturas, recibos de pago, remitos de entrada de mercadería, resguardos de retención o comunicaciones de deuda.
                        - VENTA: Si el ticket involucra a un cliente de Nesta. Esto incluye: presupuestos (ej. C00590), órdenes de trabajo, envío de archivos para imprimir, reclamos de clientes o avisos de transferencias recibidas.
                        - OTRO: Solo si es una comunicación interna, spam o algo totalmente ajeno a proveedores o clientes.
                        
                        Responde ÚNICAMENTE con una palabra: VENTA, COMPRA o OTRO."""
                        categoria = self._call_ia_with_retry(prompt_class)
                        logging.info(f"🧠 Clasificación IA (Seguimiento) para Ticket #{t_id}: {categoria}")

                        if "COMPRA" in categoria:
                            if self.RUN_TICKETS_COMPRAS:
                                logging.info(f"🧾 Ticket #{t_id} (Seguimiento) -> Agente de Compras")
                                AgenteComprasNesta().procesar_factura(ticket_id=t_id)
                            else:
                                self._notificar_descarte('helpdesk.ticket', t_id, 'RUN_TICKETS_COMPRAS')
                        elif "VENTA" in categoria:
                            if self.RUN_TICKETS_VENTAS:
                                logging.info(f"💰 Ticket #{t_id} (Seguimiento) -> Agente de Ventas")
                                AgenteVentasNesta(ticket_id=t_id).ejecutar()
                            else:
                                self._notificar_descarte('helpdesk.ticket', t_id, 'RUN_TICKETS_VENTAS')
                        
                        self.state.set_last_processed_id(t_id, max(messages))

                # B. SEGUIMIENTO DE LEADS CRM
                active_leads = conn.execute("SELECT lead_id, last_message_id FROM dispatcher_lead_state").fetchall()
                
                # N+1 Optimization
                lead_ids_followup = [l[0] for l in active_leads]
                msg_dict_leads_followup = {}
                if lead_ids_followup:
                    all_messages = self.odoo.env['mail.message'].search_read([('res_id', 'in', lead_ids_followup), ('model', '=', 'crm.lead')], ['id', 'res_id'], limit=100)
                    for m in all_messages:
                        msg_dict_leads_followup.setdefault(m['res_id'], []).append(m['id'])

                for l_id, last_msg_id in active_leads:
                    messages = [m_id for m_id in msg_dict_leads_followup.get(l_id, []) if m_id > last_msg_id]
                    if messages:
                        logging.info(f"💬 Nueva interacción en Lead activo #{l_id}")
                        lead_data = self.odoo.env['crm.lead'].read([l_id], ['custom_helpdesk_support_id', 'active', 'name', 'description'])[0]
                        if not lead_data.get('active'): continue
                        
                        # Los leads en CRM son 100% de VENTAS, pero igual chequeamos la bandera
                        if self.RUN_TICKETS_VENTAS:
                            if lead_data.get('custom_helpdesk_support_id'):
                                t_id = lead_data['custom_helpdesk_support_id'][0]
                                AgenteVentasNesta(ticket_id=t_id).ejecutar()
                            else:
                                logging.warning(f"⚠️ Lead #{l_id} no tiene Ticket asociado para seguimiento.")
                        else:
                            self._notificar_descarte('crm.lead', l_id, 'RUN_TICKETS_VENTAS')
                        
                        self.set_last_processed_lead_id(l_id, max(messages))
                
                conn.close()
            except Exception as e:
                logging.exception("Error en Seguimiento de Respuestas:")
                
        # --------------------------------------------------------------------------------
        # 6. PROACTIVIDAD (Cron de Seguimiento - 2 días, 3 días, etc.)
        # --------------------------------------------------------------------------------
        if self.RUN_PROACTIVIDAD:
            logging.info("🔎 [6/6] Ejecutando lógica de seguimiento proactivo (Drip Campaigns)...")
            try:
                CronSeguimiento().ejecutar()
            except Exception as e:
                logging.exception("Error en Proactividad:")

        logging.info("✅ CICLO DEL ORQUESTADOR COMPLETADO.")

if __name__ == "__main__":
    dispatcher = DispatcherNesta()
    dispatcher.ejecutar_ciclo_completo()
