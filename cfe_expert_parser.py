import logging
import xml.etree.ElementTree as ET
import re

class CFEExpertParser:
    """
    Expert CFE Parser for Uruguay.
    Namespace-agnostic: Works with any prefix (nsAd, ns0, etc.) using wildcard matching.
    Strictly follows DGI functional documentation (v25-2).
    """

    @staticmethod
    def parse_xml(xml_content):
        if not xml_content:
            return []
        
        try:
            if isinstance(xml_content, str):
                xml_content = xml_content.encode('utf-8')
            if xml_content.startswith(b'\xef\xbb\xbf'):
                xml_content = xml_content[3:]
                
            root = ET.fromstring(xml_content)
            
            # Wildcard search for CFE elements to ignore prefix variations
            cfe_elements = root.findall(".//{*}CFE")
            if not cfe_elements and root.tag.endswith('CFE'):
                cfe_elements = [root]
                
            parsed_docs = []
            for cfe in cfe_elements:
                doc = CFEExpertParser._process_single_cfe(cfe)
                if doc:
                    parsed_docs.append(doc)
            
            return parsed_docs

        except Exception as e:
            logging.error(f"Error parsing XML with CFEExpertParser: {e}")
            return []

    @staticmethod
    def _process_single_cfe(cfe):
        # Determine document type node using wildcard
        doc_node = None
        tags = ['eTck', 'eFact', 'eFact_Exp', 'eRem', 'eRem_Exp', 'eResg', 'eBol_Entr']
        for tag in tags:
            doc_node = cfe.find(f".//{{*}}{tag}")
            if doc_node is not None:
                break
        
        if doc_node is None:
            # Maybe it's a direct document node
            for tag in tags:
                if cfe.tag.endswith(tag):
                    doc_node = cfe
                    break
        
        if doc_node is None:
            return None

        encabezado = doc_node.find("./{*}Encabezado")
        if encabezado is None: encabezado = doc_node # Fallback search
        
        id_doc = encabezado.find("./{*}IdDoc")
        emisor = encabezado.find("./{*}Emisor")
        receptor = encabezado.find("./{*}Receptor")
        totales = encabezado.find("./{*}Totales")
        
        # 1. Basic Info
        tipo_cfe = id_doc.findtext("./{*}TipoCFE")
        serie = id_doc.findtext("./{*}Serie")
        nro = id_doc.findtext("./{*}Nro")
        fch_emis = id_doc.findtext("./{*}FchEmis")
        mnt_bruto = id_doc.findtext("./{*}MntBruto")
        fma_pago = id_doc.findtext("./{*}FmaPago")
        fch_venc = id_doc.findtext("./{*}FchVenc")
        
        # 2. Emisor - TAG CORRECTO: RUCEmisor
        rut_emisor = emisor.findtext("./{*}RUCEmisor")
        nom_emisor = emisor.findtext("./{*}RznSoc")
        
        # 3. Totales
        moneda = totales.findtext("./{*}TpoMoneda")
        
        # Odoo priority: MntPagar -> Total -> Retenido -> Crédito Fiscal (MercadoPago)
        mnt_pagar_str = totales.findtext("./{*}MntPagar")
        mnt_total_str = totales.findtext("./{*}MntTotal")
        mnt_retenido_str = totales.findtext("./{*}MntTotRetenido")
        mnt_credito_str = totales.findtext("./{*}MntTotCredFisc")
        mnt_nf_str = totales.findtext("./{*}MontoNF")
        
        monto_total = float(mnt_pagar_str) if mnt_pagar_str else (float(mnt_total_str) if mnt_total_str else 0.0)
        monto_nf = float(mnt_nf_str) if mnt_nf_str else 0.0
        
        if monto_total == 0.0:
            monto_total = float(mnt_retenido_str) if mnt_retenido_str else 0.0
        if monto_total == 0.0:
            monto_total = float(mnt_credito_str) if mnt_credito_str else 0.0

        
        # 4. Detalle de Items
        items = []
        detalle_node = doc_node.find("./{*}Detalle")
        if detalle_node is not None:
            for item_node in detalle_node.findall("./{*}Item"):
                item = {
                    'nro_linea': item_node.findtext("./{*}NroLinDet"),
                    'ind_fact': item_node.findtext("./{*}IndFact"),
                    'nom_item': item_node.findtext("./{*}NomItem"),
                    'dsc_item': item_node.findtext("./{*}DscItem"),
                    'cantidad': float(item_node.findtext("./{*}Cantidad", '0') or 0),
                    'uni_med': item_node.findtext("./{*}UniMed"),
                    'precio_unitario': float(item_node.findtext("./{*}PrecioUnitario", '0') or 0),
                    'monto_item': float(item_node.findtext("./{*}MontoItem", '0') or 0),
                    'descuento_pct': float(item_node.findtext("./{*}DescuentoPct", '0') or 0),
                    'descuento_monto': float(item_node.findtext("./{*}DescuentoMonto", '0') or 0),
                }
                items.append(item)
        
        # 5. Global Discounts
        global_discounts = []
        dsc_node = doc_node.find("./{*}DscRcgGlobal")
        if dsc_node is not None:
            for drg in dsc_node.findall("./{*}DRG_Item"):
                disc = {
                    'tpo_mov': drg.findtext("./{*}TpoMovDR"),
                    'tpo_dr': drg.findtext("./{*}TpoDR"),
                    'glosa': drg.findtext("./{*}GlosaDR"),
                    'valor': float(drg.findtext("./{*}ValorDR", '0') or 0),
                    'ind_fact': drg.findtext("./{*}IndFactDR"),
                }
                global_discounts.append(disc)

        # 6. Referencias
        referencias = []
        for ref_node in doc_node.findall(".//{*}Referencia"):
            if ref_node.find("./{*}TpoDocRef") is not None:
                r = {
                    'nro_lin_ref': ref_node.findtext("./{*}NroLinRef"),
                    'tpo_doc_ref': ref_node.findtext("./{*}TpoDocRef"),
                    'serie_ref': ref_node.findtext("./{*}Serie"),
                    'nro_cfe_ref': ref_node.findtext("./{*}NroCFERef"),
                    'fecha_cfe_ref': ref_node.findtext("./{*}FechaCFEref"),
                }
                referencias.append(r)

        # 7. Códigos de Retención (e-Resguardos)
        codigos_retencion = []
        for ret in totales.findall(".//{*}RetencPercep"):
            cod = ret.findtext("./{*}CodRet")
            if cod and cod not in codigos_retencion:
                codigos_retencion.append(cod)

        return {
            'tipo_cfe': tipo_cfe,
            'numero_factura': f"{serie}-{nro}",
            'serie': serie,
            'numero': nro,
            'fecha_factura': fch_emis,
            'vencimiento': fch_venc,
            'rut': rut_emisor,
            'razon_social': nom_emisor,
            'moneda': moneda,
            'total': monto_total,
            'monto_nf': monto_nf,
            'mnt_bruto': mnt_bruto,
            'fma_pago': fma_pago,
            'lineas': items,
            'descuentos_globales': global_discounts,
            'referencias': referencias,
            'codigos_retencion': codigos_retencion
        }

if __name__ == "__main__":
    pass
