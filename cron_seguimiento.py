import os
import logging
import odoorpc
from google import genai
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class CronSeguimiento:
    """
    Script de ejecución programada (Cron) para realizar campañas de seguimiento (Drip Campaigns)
    sobre Oportunidades inactivas en el CRM, utilizando IA para redactar los borradores.
    """
    def __init__(self):
        self.odoo = None
        self.host = os.getenv('ODOO_HOST', 'prod17.odoo.imprentadiagonal.com.uy')
        self.db = os.getenv('ODOO_DB', 'odoo17_prod')
        self.user = os.getenv('ODOO_USER', 'ylan.archimowicz@imprentadiagonal.com.uy')
        self.password = os.getenv('ODOO_PASSWORD', '9a50ca725dd8c0e451e8005982589dcdf3bf8fe7')
        self.odoo = None
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

    def _generar_borrador_ai(self, lead_id, partner_id, dias_inactivo, tipo_seguimiento):
        # Obtener contexto mínimo
        partner = self.odoo.env['res.partner'].read([partner_id], ['name'])[0]
        mensajes = self.odoo.env['mail.message'].search_read(
            [('res_id', '=', lead_id), ('model', '=', 'crm.lead')],
            ['body', 'author_id', 'date'], limit=10, order='id desc'
        )
        chatter = "\n".join([f"[{m['date']}] {m['author_id'][1] if m['author_id'] else 'Sistema'}: {m['body']}" for m in mensajes])
        
        if tipo_seguimiento == 'push_presupuesto':
            # Escala de presión comercial según los días
            escala_presion = ""
            if dias_inactivo == 2:
                escala_presion = "Presión BAJA. Objetivo: Seguimiento amable. Técnica recomendada: Enfoque en el servicio al cliente, mostrarse a disposición para resolver dudas técnicas."
            elif dias_inactivo == 4:
                escala_presion = "Presión MEDIA-BAJA. Objetivo: Generar tracción. Técnica recomendada: Urgencia leve basada en los tiempos de producción y la agenda del taller."
            elif dias_inactivo == 8:
                escala_presion = "Presión MEDIA. Objetivo: Destrabar objeciones. Técnica recomendada: Cierre alternativo o reducción de barreras (consultar proactivamente si el problema es el costo u ofrecer ajustes en especificaciones)."
            elif dias_inactivo == 16:
                escala_presion = "Presión ALTA. Objetivo: Forzar una respuesta. Técnica recomendada: Escasez o riesgo de variación de precios debido a factores externos (proveedores, stock)."
            elif dias_inactivo == 32:
                escala_presion = "Presión MÁXIMA. Objetivo: Cierre definitivo (por sí o por no). Técnica recomendada: Retirada profesional (Take it or leave it), informando el archivo inminente del presupuesto para no saturar su casilla."

            prompt = f"""
            Eres un Consultor Senior Uruguayo y experto en Cierre de Ventas. Un cliente recibió un presupuesto hace {dias_inactivo} días y está estancado sin responder.
            Tu objetivo es REVIVIR el caso o cerrarlo definitivamente aplicando la siguiente directiva:
            
            DIRECTIVA DE HOY: {escala_presion}
            
            Debes devolver EXACTAMENTE el siguiente formato:
            
            <b>[ESTRATEGIA DE CIERRE]</b><br>
            (Breve explicación dirigida al vendedor sobre qué técnica elegiste y por qué crees que funcionará hoy).<br><br>
            
            <b>[BORRADOR PARA EL CLIENTE]</b><br>
            (El mensaje exacto para enviar. Tono acorde a la presión pedida, voseo uruguayo. Usa <br> para saltos de línea. No firmes).
            
            Cliente: {partner['name']}
            Historial reciente: {chatter}
            """
        elif tipo_seguimiento == 'reclamo_ganado':
            prompt = f"""
            Eres un Consultor Senior Uruguayo. El cliente ACEPTÓ el presupuesto hace {dias_inactivo} días, pero no ha enviado los archivos de diseño finales o el comprobante de la seña.
            Redacta un mensaje CORTO para reclamar amablemente lo que falta para poder pasar a producción.
            Tono: Proactivo, servicial (voseo).
            Estructura: Saludo, recordatorio de que estamos listos para arrancar, pedido de los archivos/seña, saludo final. No firmes. Solo devuelve el texto del mensaje. Usa <br> para saltos de línea.
            Cliente: {partner['name']}
            Historial reciente: {chatter}
            """
        else:
            return ""
            
        try:
            response = self.client.models.generate_content(model=self.model_name, contents=prompt)
            return response.text
        except Exception as e:
            logging.error(f"Error generando IA: {e}")
            return "<i>(Error al generar borrador automático)</i>"

    def ejecutar(self):
        self.conectar()
        hoy = datetime.now()
        
        # ---------------------------------------------------------
        # 1. Seguimiento de Presupuestos NO Aceptados 
        # (Etapa 5: 'Esperando respuesta')
        # ---------------------------------------------------------
        leads_pendientes = self.odoo.env['crm.lead'].search_read(
            [('stage_id', '=', 5), ('active', '=', True)], 
            ['id', 'name', 'write_date', 'partner_id']
        )
        
        for lead in leads_pendientes:
            write_date = datetime.strptime(lead['write_date'], "%Y-%m-%d %H:%M:%S")
            dias_inactivo = (hoy - write_date).days
            
            # Secuencia basada en el Draw.io: 2, 3, 5 días
            if dias_inactivo in [2, 3, 5]:
                logging.info(f"⏳ Lead {lead['id']} inactivo por {dias_inactivo} días. Generando push.")
                
                # Mover a "A Responder" (Etapa 8)
                self.odoo.env['crm.lead'].write([lead['id']], {'stage_id': 8})
                
                # Generar Borrador AI
                partner_id = lead['partner_id'][0] if lead['partner_id'] else False
                borrador = self._generar_borrador_ai(lead['id'], partner_id, dias_inactivo, 'push_presupuesto') if partner_id else ""
                
                msg = f"<b>[SISTEMA - SEGUIMIENTO AUTOMÁTICO ({dias_inactivo} días)]</b><br>"
                msg += f"<b>[BORRADOR AI - PUSH LISTO PARA ENVIAR]</b><br><br>{borrador}"
                
                if dias_inactivo == 5:
                    msg += "<br><br><i>⚠️ Este es el último aviso programado. Si no hay respuesta, pasar a Perdido.</i>"
                    
                self.odoo.env['mail.message'].create({
                    'model': 'crm.lead', 'res_id': lead['id'], 
                    'body': msg, 'message_type': 'comment', 'subtype_id': 2
                })

        # ---------------------------------------------------------
        # 2. Seguimiento de Presupuestos Ganados SIN OT
        # (Etapa 6: 'Ganado sin OT')
        # ---------------------------------------------------------
        leads_ganados_sin_ot = self.odoo.env['crm.lead'].search_read(
            [('stage_id', '=', 6), ('active', '=', True)], 
            ['id', 'name', 'write_date', 'partner_id']
        )
        
        for lead in leads_ganados_sin_ot:
            write_date = datetime.strptime(lead['write_date'], "%Y-%m-%d %H:%M:%S")
            dias_inactivo = (hoy - write_date).days
            
            # Secuencia: 2, 4, 7 días, y luego cada 7 días
            if dias_inactivo in [2, 4, 7] or (dias_inactivo > 7 and dias_inactivo % 7 == 0):
                logging.info(f"⏳ Lead Ganado {lead['id']} estancado por {dias_inactivo} días. Reclamando info.")
                
                partner_id = lead['partner_id'][0] if lead['partner_id'] else False
                borrador = self._generar_borrador_ai(lead['id'], partner_id, dias_inactivo, 'reclamo_ganado') if partner_id else ""
                
                msg = f"<b>[SISTEMA - RECLAMO DE INFO ({dias_inactivo} días)]</b><br>"
                msg += f"Faltan archivos o el pago de la seña.<br><br><b>[BORRADOR AI - RECLAMO]</b><br><br>{borrador}"
                
                self.odoo.env['mail.message'].create({
                    'model': 'crm.lead', 'res_id': lead['id'], 
                    'body': msg, 'message_type': 'comment', 'subtype_id': 2
                })

if __name__ == "__main__":
    cron = CronSeguimiento()
    cron.ejecutar()
