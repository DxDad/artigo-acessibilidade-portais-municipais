#!/usr/bin/env python3
# scripts/gerar_anexo_visual_tcc.py

import csv
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Tuple

import pymysql
from PIL import Image
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas


# =========================
# Configuração por ambiente
# =========================

BD_HOST = os.environ["BD_HOST"]
BD_PORTA = int(os.environ["BD_PORTA"])
BD_NOME = os.environ["BD_NOME"]
BD_USUARIO = os.environ["BD_USUARIO"]
BD_SENHA = os.environ["BD_SENHA"]

BD_TABELA_BASE_TCC = os.environ.get("BD_TABELA_BASE_TCC", "tcc_unifal_base_amostral").strip()

CAMINHO_EVIDENCIAS_TCC = Path(os.environ["CAMINHO_EVIDENCIAS_TCC"]).expanduser()

MODO_FILA = os.environ.get("MODO_FILA", "primeiros_n").strip().lower()
PRIMEIROS_N = int(os.environ.get("PRIMEIROS_N", "5"))

ROTULO_EXECUCAO = os.environ.get("ROTULO_EXECUCAO", "anexo_visual_tcc").strip() or "anexo_visual_tcc"
NOME_ARQUIVO = os.environ.get("NOME_ARQUIVO", "anexo_a_evidencias_visuais_amostra").strip() or "anexo_a_evidencias_visuais_amostra"
FALHAR_SE_INCONSISTENTE = os.environ.get("FALHAR_SE_INCONSISTENTE", "sim").strip().lower() == "sim"

# Compactação das imagens embutidas no PDF.
# As imagens originais em PNG/JPG permanecem intactas na pasta consolidadas.
COMPACTAR_IMAGENS_PDF = os.environ.get("COMPACTAR_IMAGENS_PDF", "sim").strip().lower() == "sim"
QUALIDADE_JPEG = int(os.environ.get("QUALIDADE_JPEG", "85"))
LARGURA_MAXIMA_IMAGEM_PDF = int(os.environ.get("LARGURA_MAXIMA_IMAGEM_PDF", "1600"))

DIR_CONSOLIDADAS = CAMINHO_EVIDENCIAS_TCC / "capturas_homepages" / "consolidadas"
DIR_SAIDA = CAMINHO_EVIDENCIAS_TCC / "pdf_anexos"

DATA_EXEC = datetime.now().strftime("%Y%m%d_%H%M%S")
PDF_SAIDA = DIR_SAIDA / f"{NOME_ARQUIVO}_{DATA_EXEC}.pdf"
CSV_CONFERENCIA = DIR_SAIDA / f"{NOME_ARQUIVO}_{DATA_EXEC}_conferencia.csv"
DIR_TEMP = DIR_SAIDA / f"_temp_{NOME_ARQUIVO}_{DATA_EXEC}"


# =========================
# Fonte
# =========================

def fonte_pdf() -> str:
    """
    Usa Arial se houver fonte instalada no servidor.
    Caso contrário, usa Helvetica, fonte padrão do PDF/ReportLab.
    """
    candidatos = [
        "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/arial.ttf",
        "/usr/share/fonts/truetype/microsoft/Arial.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]

    for caminho in candidatos:
        if os.path.exists(caminho):
            try:
                pdfmetrics.registerFont(__import__("reportlab.pdfbase.ttfonts").pdfbase.ttfonts.TTFont("ArialCustom", caminho))
                return "ArialCustom"
            except Exception:
                pass

    return "Helvetica"


FONTE = fonte_pdf()


# =========================
# Banco
# =========================

def conectar_banco():
    return pymysql.connect(
        host=BD_HOST,
        port=BD_PORTA,
        user=BD_USUARIO,
        password=BD_SENHA,
        database=BD_NOME,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )


def carregar_base() -> List[Dict[str, Any]]:
    limite = ""
    if MODO_FILA == "primeiros_n":
        limite = f"LIMIT {PRIMEIROS_N}"
    elif MODO_FILA == "todos":
        limite = ""
    else:
        raise ValueError(f"modo_fila inválido: {MODO_FILA}. Use 'todos' ou 'primeiros_n'.")

    sql = f"""
        SELECT
            codigo_municipio_completo,
            codigo_uf,
            nome_uf,
            codigo_municipio,
            nome_municipio,
            regiao,
            url,
            site_encontrado,
            url_confiavel,
            status_validacao_url,
            observacao,
            populacao_residente_2022,
            area_territorial_km2,
            densidade_demografica_hab_km2,
            chave_aleatoria
        FROM `{BD_TABELA_BASE_TCC}`
        WHERE TRIM(status_validacao_url) = 'Apto'
        ORDER BY
            regiao ASC,
            CAST(REPLACE(chave_aleatoria, ',', '.') AS DECIMAL(20,16)) ASC,
            codigo_municipio_completo ASC
        {limite}
    """

    with conectar_banco() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()


# =========================
# Imagens
# =========================

def indexar_imagens() -> Tuple[Dict[str, List[Path]], List[Path]]:
    """
    Indexa imagens pelo código IBGE presente no nome:
    municipio_uf_5107909_homepage_20260617_1900.png
    """
    if not DIR_CONSOLIDADAS.exists():
        raise FileNotFoundError(f"Pasta de consolidadas não encontrada: {DIR_CONSOLIDADAS}")

    padrao = re.compile(r"_(\d{7})_homepage_\d{8}_\d{4}\.(png|jpg|jpeg)$", re.IGNORECASE)

    indice: Dict[str, List[Path]] = {}
    imagens_sem_codigo: List[Path] = []

    for img in sorted(DIR_CONSOLIDADAS.rglob("*")):
        if not img.is_file():
            continue
        if img.suffix.lower() not in [".png", ".jpg", ".jpeg"]:
            continue

        m = padrao.search(img.name)
        if not m:
            imagens_sem_codigo.append(img)
            continue

        codigo = m.group(1)
        indice.setdefault(codigo, []).append(img)

    return indice, imagens_sem_codigo


def extrair_data_captura_arquivo(path: Path) -> str:
    m = re.search(r"_homepage_(\d{8})_(\d{4})\.", path.name, re.IGNORECASE)
    if not m:
        return ""

    data_raw = m.group(1)

    try:
        dt = datetime.strptime(data_raw, "%Y%m%d")
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return ""


# =========================
# Texto e layout
# =========================

def texto_seguro(valor: Any) -> str:
    if valor is None:
        return ""
    return str(valor).strip()


def chave_formatada(valor: Any) -> str:
    s = texto_seguro(valor)
    if not s:
        return ""
    return s.replace(".", ",")


def linhas_legenda(row: Dict[str, Any], imagem: Path) -> List[str]:
    municipio = texto_seguro(row["nome_municipio"])
    uf = texto_seguro(row["nome_uf"])
    regiao = texto_seguro(row["regiao"])
    codigo = texto_seguro(row["codigo_municipio_completo"])
    chave = chave_formatada(row["chave_aleatoria"])
    url = texto_seguro(row["url"])
    data_captura = extrair_data_captura_arquivo(imagem)

    return [
        f"Código IBGE: {codigo} | Chave aleatória: {chave}",
        f"Município: {municipio} | UF: {uf} | Região: {regiao}",
        f"Data da captura: {data_captura} | URL: {url}",
    ]


def desenhar_texto_ajustado(c: canvas.Canvas, texto: str, x: float, y: float, largura_max: float, fonte: str, tamanho: int):
    """
    Reduz levemente o tamanho da fonte se uma linha longa não couber.
    Mantém uma única linha para preservar a organização definida.
    """
    tamanho_atual = tamanho
    while tamanho_atual >= 8:
        if pdfmetrics.stringWidth(texto, fonte, tamanho_atual) <= largura_max:
            break
        tamanho_atual -= 0.5

    c.setFont(fonte, tamanho_atual)
    c.drawString(x, y, texto)


def desenhar_capa(c: canvas.Canvas):
    largura, altura = landscape(A4)

    c.setFont(FONTE, 20)
    titulo = "Anexo A - Evidências Visuais da Amostra"
    tw = pdfmetrics.stringWidth(titulo, FONTE, 20)
    c.drawString((largura - tw) / 2, altura / 2 + 20, titulo)

    c.setFont(FONTE, 12)
    subtitulo = "Acessibilidade Digital em Portais de Prefeituras Brasileiras"
    sw = pdfmetrics.stringWidth(subtitulo, FONTE, 12)
    c.drawString((largura - sw) / 2, altura / 2 - 10, subtitulo)

    c.showPage()


def preparar_imagem_para_pdf(imagem: Path) -> Path:
    """
    Cria uma versão temporária JPEG para reduzir o tamanho do PDF.
    Não altera a imagem original.
    """
    if not COMPACTAR_IMAGENS_PDF:
        return imagem

    DIR_TEMP.mkdir(parents=True, exist_ok=True)

    destino = DIR_TEMP / f"{imagem.stem}.jpg"

    if destino.exists():
        return destino

    with Image.open(imagem) as im:
        im = im.convert("RGB")

        if LARGURA_MAXIMA_IMAGEM_PDF > 0 and im.width > LARGURA_MAXIMA_IMAGEM_PDF:
            proporcao = LARGURA_MAXIMA_IMAGEM_PDF / im.width
            nova_altura = int(im.height * proporcao)
            im = im.resize((LARGURA_MAXIMA_IMAGEM_PDF, nova_altura), Image.Resampling.LANCZOS)

        im.save(
            destino,
            "JPEG",
            quality=QUALIDADE_JPEG,
            optimize=True,
            progressive=True,
        )

    return destino
    

def desenhar_pagina(c: canvas.Canvas, row: Dict[str, Any], imagem: Path, numero_item: int):
    largura, altura = landscape(A4)
    imagem_pdf = preparar_imagem_para_pdf(imagem)

    margem_x = 28
    margem_topo = 24
    margem_base = 24

    legenda_altura = 62
    gap = 12

    area_img_x = margem_x
    area_img_y = margem_base + legenda_altura + gap
    area_img_w = largura - (2 * margem_x)
    area_img_h = altura - margem_topo - area_img_y

    with Image.open(imagem_pdf) as im:
        img_w, img_h = im.size

    escala = min(area_img_w / img_w, area_img_h / img_h)
    draw_w = img_w * escala
    draw_h = img_h * escala

    img_x = margem_x + (area_img_w - draw_w) / 2
    img_y = area_img_y + (area_img_h - draw_h) / 2

    c.drawImage(str(imagem_pdf), img_x, img_y, width=draw_w, height=draw_h, preserveAspectRatio=True, anchor="c")

    # Linha fina separando imagem e legenda
    c.setLineWidth(0.4)
    c.line(margem_x, margem_base + legenda_altura + 4, largura - margem_x, margem_base + legenda_altura + 4)

    linhas = linhas_legenda(row, imagem)

    y = margem_base + legenda_altura - 14
    largura_texto = largura - (2 * margem_x)

    c.setFont(FONTE, 12)
    for linha in linhas:
        desenhar_texto_ajustado(c, linha, margem_x, y, largura_texto, FONTE, 12)
        y -= 16

    # Número discreto do item no canto inferior direito
    c.setFont(FONTE, 9)
    rodape = f"Item {numero_item}"
    rw = pdfmetrics.stringWidth(rodape, FONTE, 9)
    c.drawString(largura - margem_x - rw, 10, rodape)

    c.showPage()


# =========================
# Validação e geração
# =========================

def validar_correspondencia(base: List[Dict[str, Any]], indice: Dict[str, List[Path]]) -> Tuple[List[str], List[str]]:
    codigos_base = [texto_seguro(r["codigo_municipio_completo"]) for r in base]
    set_base = set(codigos_base)
    set_imgs = set(indice.keys())

    faltantes = sorted([c for c in codigos_base if c not in set_imgs])
    duplicados = sorted([c for c, imgs in indice.items() if c in set_base and len(imgs) > 1])

    return faltantes, duplicados


def gerar_csv_conferencia(registros: List[Dict[str, Any]]):
    campos = [
        "ordem_pdf",
        "codigo_municipio_completo",
        "nome_municipio",
        "nome_uf",
        "regiao",
        "chave_aleatoria",
        "url",
        "arquivo_imagem",
        "caminho_imagem",
        "captura",
    ]

    with open(CSV_CONFERENCIA, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        for r in registros:
            w.writerow({k: r.get(k, "") for k in campos})


def main():
    DIR_SAIDA.mkdir(parents=True, exist_ok=True)

    print("Gerando Anexo Visual TCC")
    print(f"Modo fila: {MODO_FILA}")
    print(f"Primeiros N: {PRIMEIROS_N}")
    print(f"Pasta de imagens: {DIR_CONSOLIDADAS}")
    print(f"Pasta de saída: {DIR_SAIDA}")

    base = carregar_base()
    print(f"Registros carregados do banco: {len(base)}")

    if not base:
        print("ERRO: nenhum registro carregado.")
        sys.exit(1)

    indice, imagens_sem_codigo = indexar_imagens()
    print(f"Códigos com imagem indexada: {len(indice)}")
    print(f"Imagens sem código reconhecido: {len(imagens_sem_codigo)}")

    faltantes, duplicados = validar_correspondencia(base, indice)

    if faltantes:
        print("ERRO: municípios da base sem imagem:")
        for c in faltantes:
            print(f" - {c}")

    if duplicados:
        print("ERRO: códigos com mais de uma imagem:")
        for c in duplicados:
            print(f" - {c}")
            for img in indice[c]:
                print(f"   {img}")

    if (faltantes or duplicados) and FALHAR_SE_INCONSISTENTE:
        print("Execução interrompida por inconsistência entre base e imagens.")
        sys.exit(1)

    c = canvas.Canvas(str(PDF_SAIDA), pagesize=landscape(A4))
    c.setTitle("Anexo A - Evidências Visuais da Amostra")
    c.setAuthor("Danilo Silva de Souza")

    desenhar_capa(c)

    registros_conferencia = []

    for i, row in enumerate(base, start=1):
        codigo = texto_seguro(row["codigo_municipio_completo"])
        imagens = indice.get(codigo, [])

        if not imagens:
            continue

        imagem = imagens[0]

        desenhar_pagina(c, row, imagem, i)

        registros_conferencia.append({
            "ordem_pdf": i,
            "codigo_municipio_completo": codigo,
            "nome_municipio": texto_seguro(row["nome_municipio"]),
            "nome_uf": texto_seguro(row["nome_uf"]),
            "regiao": texto_seguro(row["regiao"]),
            "chave_aleatoria": chave_formatada(row["chave_aleatoria"]),
            "url": texto_seguro(row["url"]),
            "arquivo_imagem": imagem.name,
            "caminho_imagem": str(imagem),
            "captura": extrair_data_captura_arquivo(imagem),
        })

    c.save()
    gerar_csv_conferencia(registros_conferencia)

    if COMPACTAR_IMAGENS_PDF and DIR_TEMP.exists():
        import shutil
        shutil.rmtree(DIR_TEMP, ignore_errors=True)

    print("PDF gerado:")
    print(PDF_SAIDA)
    print("CSV de conferência gerado:")
    print(CSV_CONFERENCIA)
    print(f"Total de páginas de evidência: {len(registros_conferencia)}")
    print("Concluído.")


if __name__ == "__main__":
    main()
