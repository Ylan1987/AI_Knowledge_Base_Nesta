import os
import logging
import requests
import base64
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta
from dotenv import load_dotenv
import xml.etree.ElementTree as ET
from nesta_senior_core import NestaSeniorCore
from agente_compras import AgenteComprasNesta

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("UCFESyncBot")

class UCFESyncBot:
    """
    BOT DE SINCRONIZACIÓN TOTAL (UCFE -> ODOO)
    Barre los últimos 3 días, baja XML/PDF y audita en Odoo usando el Agente Senior.
    """
    def __init__(self):
        self.url_list = "https://prod8212.ucfe.com.uy/Query116/WebServicesListadosFE.svc/rest/"
        self.url_query = "https://prod8212.ucfe.com.uy/Query116/WebServicesFE.svc/rest/"
        self.user = "213382910014"
        self.password = os.getenv('UCFE_PASSWORD', "XEePX3X5m5E8pcfgVgp4jA==")
        self.auth = HTTPBasicAuth(self.user, self.password)
        self.agente_senior = AgenteComprasNesta()

    def _descargar_xml_puro(self, cfe_node, tipo_code=None):
        """Baja el XML legal usando los datos del nodo de la lista."""
        rut_emisor = cfe_node.findtext(".//{*}RucEmisor")
        tipo = tipo_code if tipo_code else cfe_node.findtext(".//{*}TipoCfe")
        serie = cfe_node.findtext(".//{*}Serie")
        nro = cfe_node.findtext(".//{*}Numero")
        
        # Mapeo rápido si viene el nombre
        tipo_map = {'ETicket': '101', 'EFactura': '111', 'ERemito': '181', 'EResguardo': '182', 
                    'NotaCreditoETicket': '102', 'NotaCreditoEFactura': '112'}
        tipo_final = tipo_map.get(tipo, tipo)

        logger.info(f"📥 Descargando XML de {rut_emisor} {tipo_final} {serie}-{nro}...")
        url = self.url_query + "ObtenerCfeRecibido"
        # IMPORTANTE: tipoCfe y numeroCfe DEBEN ser enteros en el JSON para REST
        payload = {
            "rut": self.user, 
            "rutRecibido": rut_emisor, 
            "tipoCfe": int(tipo_final), 
            "serieCfe": serie, 
            "numeroCfe": int(nro)
        }
        
        try:
            res = requests.post(url, json=payload, auth=self.auth, timeout=20)
            if res.status_code == 200:
                content = res.text.strip().replace('\ufeff', '')
                root = ET.fromstring(content)
                # El XML firmado viene dentro del tag <Xml> o directamente en el body dependiendo del WS
                xml_signed = root.findtext(".//{*}Xml") or root.text
                return xml_signed
            else:
                logger.error(f"Error descargando XML ({res.status_code}): {res.text}")
        except Exception as e:
            logger.error(f"Excepción descargando XML: {e}")
            return None

    def _descargar_pdf_puro(self, cfe_node, tipo_code=None):
        """Baja el PDF oficial."""
        rut_emisor = cfe_node.findtext(".//{*}RucEmisor")
        tipo = tipo_code if tipo_code else cfe_node.findtext(".//{*}TipoCfe")
        serie = cfe_node.findtext(".//{*}Serie")
        nro = cfe_node.findtext(".//{*}Numero")
        
        # Mapeo rápido si viene el nombre
        tipo_map = {'ETicket': '101', 'EFactura': '111', 'ERemito': '181', 'EResguardo': '182', 
                    'NotaCreditoETicket': '102', 'NotaCreditoEFactura': '112'}
        tipo_final = tipo_map.get(tipo, tipo)

        logger.info(f"📥 Descargando PDF de {rut_emisor} {tipo_final} {serie}-{nro}...")
        url = self.url_query + "ObtenerPdfCfeRecibido"
        # IMPORTANTE: tipoCfe y numeroCfe DEBEN ser enteros en el JSON para REST
        payload = {
            "rut": self.user, 
            "rutRecibido": rut_emisor, 
            "tipoCfe": int(tipo_final), 
            "serieCfe": serie, 
            "numeroCfe": int(nro)
        }
        
        try:
            res = requests.post(url, json=payload, auth=self.auth, timeout=20)
            if res.status_code == 200:
                content = res.text.strip().replace('\ufeff', '')
                if "base64Binary" in content or "<base64Binary" in content:
                    root = ET.fromstring(content)
                    return root.text
                return res.text
            else:
                logger.error(f"Error descargando PDF ({res.status_code}): {res.text}")
        except Exception as e:
            logger.error(f"Excepción descargando PDF: {e}")
            return None

    def sincronizar_todo(self, dias=30):
        """Procesa todos los CFEs encontrados en el rango de días."""
        logger.info(f"🚀 Iniciando Sincronización de Producción Total - Mirando {dias} días...")
        self.agente_senior.conectar()
        
        now = datetime.now()
        f_desde = (now - timedelta(days=dias)).strftime("%Y%m%d")
        f_hasta = now.strftime("%Y%m%d")
        
        payload = {
            "rut": self.user, "rutEmisor": "", "fechaDesde": f_desde, "fechaHasta": f_hasta, "tipoCfe": 0, "pageSize": 100
        }

        try:
            res = requests.post(self.url_list + "ObtenerCfeRecibidosInicial", json=payload, auth=self.auth, timeout=40)
            if res.status_code != 200: 
                logger.error(f"Error UCFE: {res.status_code}")
                return
            
            content = res.text.strip().replace('\ufeff', '')
            root = ET.fromstring(content)
            cfes = root.findall(".//{*}CfeRecibido")
            
            logger.info(f"Se encontraron {len(cfes)} CFEs en el listado inicial.")
            ns = {'a': 'http://schemas.datacontract.org/2004/07/TrxServer.Cfe.Common.Entities'}
            for cfe in cfes:
                try:
                    tipo = cfe.findtext("a:TipoCfe", namespaces=ns)
                    serie = cfe.findtext("a:Serie", namespaces=ns)
                    nro = cfe.findtext("a:Numero", namespaces=ns)
                    emisor = cfe.findtext("a:NombreFantasiaRucEmisor", namespaces=ns)
                    rut_emisor = cfe.findtext("a:RucEmisor", namespaces=ns)
                    
                    # Normalizar tipo
                    tipo_map = {'ETicket': '101', 'EFactura': '111', 'ERemito': '181', 'EResguardo': '182', 
                                'NotaCreditoETicket': '102', 'NotaCreditoEFactura': '112'}
                    tipo_code = tipo_map.get(tipo, tipo)

                    # FILTRO PREVENTIVO (Optimización 09/06): No descargar si ya está procesado
                    doc_state = self.agente_senior._get_doc_state(str(rut_emisor), str(tipo_code), str(serie), str(nro))
                    if doc_state['xml_ok']:
                        continue

                    logger.info(f"🎯 Procesando {tipo} ({serie}-{nro}) de {emisor}...")
                    
                    xml_legal = self._descargar_xml_puro(cfe, tipo_code=tipo_code)
                    pdf_oficial = self._descargar_pdf_puro(cfe, tipo_code=tipo_code)
                    
                    attachments = []
                    if xml_legal:
                        attachments.append({'name': f"UCFE_{serie}_{nro}.xml", 'datas': base64.b64encode(xml_legal.encode()).decode(), 'mimetype': 'application/xml'})
                    if pdf_oficial:
                        attachments.append({'name': f"UCFE_{serie}_{nro}.pdf", 'datas': pdf_oficial, 'mimetype': 'application/pdf'})
                    
                    if attachments:
                        email_mock = {'from': emisor, 'attachments': attachments}
                        res_ids = self.agente_senior.procesar_desde_zoho(email_mock)
                        logger.info(f"✅ Completado {tipo_code}. IDs Odoo: {res_ids}")
                except Exception as item_error:
                    logger.error(f"❌ Error procesando documento individual {serie if 'serie' in locals() else 'S/N'}-{nro if 'nro' in locals() else 'S/N'}: {item_error}")
                    continue

        except Exception as e:
            logger.error(f"Falla en sincronización UCFE: {e}")

if __name__ == "__main__":
    pass
