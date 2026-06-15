# BMG: Base de Conocimiento de Productos y Reglas de Negocio

Este documento es el repositorio central de la lógica técnica y comercial de BMG. Define qué es cada producto, qué variables lo componen y cuáles son las prohibiciones o límites técnicos que la IA debe respetar.

---

## 🛠️ Esquema de Datos General (Estructura)
Cada producto agregado a esta base debe seguir esta estructura para que la IA pueda procesarlo:

1.  **Definición:** Qué es el producto y para qué sirve.
2.  **Variables de Entrada (Input):** Datos que el vendedor o la IA deben pedir al cliente.
3.  **Lógica de Compatibilidad (Restricciones):** Reglas de "Si pasa X, entonces NO se puede Y".
4.  **Lógica de Producción (OT):** Cómo se traduce la selección a instrucciones de taller.
5.  **Plantilla de Presupuesto:** Cómo se le presenta la información al cliente.

---

## 📂 Producto 1: Carpetas

### 1. Definición
Producto estructural que consiste en una cartulina impresa y doblada, con o sin bolsillos internos, diseñada para contener documentos.

### 2. Variables de Entrada
*   **Cantidad:** (50 a 20.000 unidades).
*   **Tiempo de entrega:** (3 días "Express" o 10 días "Estándar").
*   **Geometría:** 
    *   Tamaño Cerrado (mm).
    *   Borde de doblado (Largo o Corto).
    *   Orientación (Vertical u Horizontal).
*   **Material (Papel):**
    *   Coteado (300gr, 350gr).
    *   Cartulina (250gr, 270gr).
    *   Obra (210gr, 240gr).
    *   Reciclado (240gr).
    *   Ficha Color (150gr).
    *   Cartulina Magic Color Textura (180gr).
*   **Arquitectura:** 
    *   Bolsillo (Si/No).
    *   Tipo de Bolsillo (Estándar c/s corte tarjeta, Troquel especial, Fuelle).
    *   Oreja (Si/No).
    *   Visagra (Si/No).
*   **Terminaciones:**
    *   Laminado (Mate/Brillo).
    *   Barniz (Mate/Brillo SF/DF, UV Pleno/Sectorizado).

### 3. Lógica de Compatibilidad (Prohibiciones)
*   **Regla de Oro del Papel y Color:**
    *   Si Papel = `Ficha Color` o `Magic Color` -> Se permiten colores de la paleta técnica.
    *   Si Papel = `Coteado`, `Obra`, `Cartulina` o `Reciclado` -> **SOLO BLANCO**.
*   **Lógica de Superficie (Cálculo de Viabilidad):**
    *   *Fórmula Ancho Abierto:* `(Ancho Cerrado * 2) + (15mm si lleva Bolsillo o Visagra)`.
    *   *Fórmula Superficie Total (m²):* `(Ancho Abierto / 1000) * (Alto Abierto / 1000) * Cantidad`.
*   **Restricción de Troquelado:**
    *   Si `Superficie Total < 18 m²` -> **PROHIBIDO** Troquelado de dificultad (orejas, troqueles especiales). Solo se permiten cortes rectos.
    *   Si `Entrega = 3 días` -> **PROHIBIDO** Troquelado, Orejas y Visagra.
*   **Restricción de Barniz Industrial:**
    *   Si `Superficie Total < 35 m²` -> **PROHIBIDO** Barniz de máquina o UV industrial.
    *   Si `Entrega = 3 días` -> **PROHIBIDO** todo tipo de Barniz.

### 4. Salida Técnica (Orden de Trabajo - OT)
Formato estricto: 
`#Carpetas / Unidades: [Cant] / Papel: [Papel] [Color] / Cerrado: [Dim] / Abierto: [Calc] / Orientación: [Or] / Doblez: [Borde] / Impresión: [Inks] [Faces] / Bolsillo: [Tipo] / Terminaciones: [Lista]`

---

## 📂 Producto 2: [Espacio para mañana - Ej. Libros]
*(Mañana completaremos la lógica de Libros, Folletos, etc., siguiendo este mismo esquema de restricciones matemáticas y técnicas).*

---
**Nota para la IA:** Antes de generar cualquier respuesta a un cliente sobre Carpetas, verificar siempre la sección "Lógica de Compatibilidad" de este documento.
