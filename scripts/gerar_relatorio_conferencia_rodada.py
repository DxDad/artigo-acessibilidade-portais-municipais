#!/usr/bin/env python3
# scripts/gerar_relatorio_conferencia_rodada.py
# Gera relatório Markdown de homologação/conferência operacional de rodadas do TCC Unifal.

import os
import sys
import json
import math
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple, Optional

import pymysql


# =====================
# Configuração / ENV
# =====================
RODADA = os.environ.get("RODADA", os.environ.get("RODADA_ANALISADA", os.environ.get("RODADA_HOMOLOGACAO", "r00"))).strip() or "r00"
ANEXO_LETRA = os.environ.get("ANEXO_LETRA", "B").strip().upper() or "B"
ANEXO_SLUG = ANEXO_LETRA.lower()
TIPO_RELATORIO = "Homologação" if RODADA.lower() == "r00" else "Conferência"

BD_HOST = os.environ["BD_HOST"]
BD_PORTA = int(os.environ.get("BD_PORTA", "3306"))
BD_NOME = os.environ["BD_NOME"]
BD_USUARIO = os.environ["BD_USUARIO"]
BD_SENHA = os.environ["BD_SENHA"]

TABELA_BASE = os.environ.get("BD_TABELA_BASE_TCC", "tcc_unifal_base_amostral").strip() or "tcc_unifal_base_amostral"
TABELA_EXEC = os.environ.get("BD_TABELA_EXECUCOES_TCC", "tcc_unifal_execucoes").strip() or "tcc_unifal_execucoes"
TABELA_LOG = os.environ.get("BD_TABELA_LOG_TCC", "tcc_unifal_logs_validacao").strip() or "tcc_unifal_logs_validacao"

CAMINHO_EVIDENCIAS_TCC = os.environ.get("CAMINHO_EVIDENCIAS_TCC", ".").rstrip("/")
OUT_DIR = os.environ.get(
    "CAMINHO_RELATORIO_CONFERENCIA",
    os.environ.get(
        "CAMINHO_RELATORIO_HOMOLOGACAO",
        os.path.join(CAMINHO_EVIDENCIAS_TCC, "relatorios_conferencia", RODADA),
    ),
).rstrip("/")

FALHAR_SE_REPROVADA = str(os.environ.get("FALHAR_SE_REPROVADA", "nao")).strip().lower() in ("1", "true", "yes", "sim", "s")
LIMITE_FALHA_CRITICA = float(os.environ.get("LIMITE_FALHA_CRITICA", "0.05"))

# Distribuição definida no plano amostral validado.
DISTRIBUICAO_REGIONAL_ESPERADA = {
    "Centro-Oeste": 31,
    "Nordeste": 116,
    "Norte": 30,
    "Sudeste": 108,
    "Sul": 77,
}

FERRAMENTAS = ["amaweb", "accessmonitor"]

# Falhas de sistema: eventos atribuíveis à automação, ambiente ou integração, e não
# simplesmente à ausência de nota por comportamento do site ou da ferramenta.
CRITICAL_STATUS = {
    "erro_critico",
    "erro_sistema",
    "erro_python",
    "erro_banco",
    "erro_workflow",
    "falha_extensao",
    "extensao_nao_carregada",
    "service_worker_nao_encontrado",
}
CRITICAL_PATTERNS = [
    "NameError",
    "SyntaxError",
    "IndentationError",
    "ModuleNotFoundError",
    "ImportError",
    "ProgrammingError",
    "OperationalError",
    "Traceback",
    "CAMINHO_EXTENSAO_ACCESSMONITOR",
    "manifest.json da extensão",
    "service_worker_nao_encontrado",
]


# =====================
# Banco
# =====================
def conectar():
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
        read_timeout=60,
        write_timeout=60,
    )


def q1(conn, sql: str, params: Tuple[Any, ...] = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if not row:
        return None
    return next(iter(row.values()))


def qall(conn, sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def table_columns(conn, table: str) -> List[str]:
    rows = qall(
        conn,
        """
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (table,),
    )
    return [r["COLUMN_NAME"] for r in rows]


# =====================
# Formatação
# =====================
def agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ts_nome() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def pct(num: float, den: float) -> float:
    if den in (0, None):
        return 0.0
    return (float(num) / float(den)) * 100.0


def fmt_pct(v: float) -> str:
    return f"{v:.2f}%".replace(".", ",")


def fmt_num(v: Any) -> str:
    if v is None:
        return "0"
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return f"{v:.2f}".replace(".", ",")
    return str(v)


def md_table(headers: List[str], rows: List[List[Any]]) -> str:
    out = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        safe = []
        for cell in row:
            text = "" if cell is None else str(cell)
            text = text.replace("\n", " ").replace("|", "\\|")
            safe.append(text)
        out.append("| " + " | ".join(safe) + " |")
    return "\n".join(out)


def situacao(ok: bool) -> str:
    return "Atendido" if ok else "Não atendido"


# =====================
# Critérios
# =====================
def criterio_1_cobertura(conn) -> Dict[str, Any]:
    total_base = q1(conn, f"SELECT COUNT(*) FROM `{TABELA_BASE}` WHERE url IS NOT NULL AND TRIM(url) <> ''") or 0
    total_exec = q1(conn, f"SELECT COUNT(DISTINCT codigo_municipio_completo) FROM `{TABELA_EXEC}` WHERE rodada = %s", (RODADA,)) or 0
    faltantes = qall(
        conn,
        f"""
        SELECT b.codigo_municipio_completo, b.nome_municipio, b.nome_uf, b.regiao
        FROM `{TABELA_BASE}` b
        LEFT JOIN `{TABELA_EXEC}` e
          ON e.codigo_municipio_completo = b.codigo_municipio_completo
         AND e.rodada = %s
        WHERE b.url IS NOT NULL AND TRIM(b.url) <> ''
          AND e.codigo_municipio_completo IS NULL
        ORDER BY b.regiao, b.nome_uf, b.nome_municipio
        """,
        (RODADA,),
    )
    cobertura = pct(total_exec, total_base)
    ok = (total_base > 0 and total_exec == total_base and len(faltantes) == 0)
    return {
        "id": 1,
        "nome": "Cobertura da base amostral",
        "ok": ok,
        "resumo": f"{total_exec}/{total_base} municípios com execução ({fmt_pct(cobertura)}).",
        "dados": {"total_base": total_base, "total_exec": total_exec, "cobertura_pct": cobertura, "faltantes": faltantes[:30], "faltantes_total": len(faltantes)},
    }


def criterio_2_unicidade(conn) -> Dict[str, Any]:
    dup = qall(
        conn,
        f"""
        SELECT rodada, codigo_municipio_completo, COUNT(*) AS total
        FROM `{TABELA_EXEC}`
        WHERE rodada = %s
        GROUP BY rodada, codigo_municipio_completo
        HAVING COUNT(*) > 1
        ORDER BY total DESC, codigo_municipio_completo
        """,
        (RODADA,),
    )
    ok = len(dup) == 0
    return {
        "id": 2,
        "nome": "Unicidade dos registros por município e rodada",
        "ok": ok,
        "resumo": f"{len(dup)} duplicidade(s) encontrada(s).",
        "dados": {"duplicidades_total": len(dup), "duplicidades": dup[:30]},
    }


def criterio_3_vinculacao(conn) -> Dict[str, Any]:
    sem_base = qall(
        conn,
        f"""
        SELECT e.codigo_municipio_completo
        FROM `{TABELA_EXEC}` e
        LEFT JOIN `{TABELA_BASE}` b
          ON b.codigo_municipio_completo = e.codigo_municipio_completo
        WHERE e.rodada = %s
          AND b.codigo_municipio_completo IS NULL
        ORDER BY e.codigo_municipio_completo
        """,
        (RODADA,),
    )
    total_exec = q1(conn, f"SELECT COUNT(*) FROM `{TABELA_EXEC}` WHERE rodada = %s", (RODADA,)) or 0
    ok = len(sem_base) == 0
    return {
        "id": 3,
        "nome": "Vinculação entre execução e base amostral",
        "ok": ok,
        "resumo": f"{len(sem_base)} registro(s) de execução sem correspondência na base, em {total_exec} execução(ões).",
        "dados": {"execucoes_total": total_exec, "sem_base_total": len(sem_base), "sem_base": sem_base[:30]},
    }


def criterio_4_regional(conn) -> Dict[str, Any]:
    rows = qall(
        conn,
        f"""
        SELECT b.regiao, COUNT(DISTINCT e.codigo_municipio_completo) AS total
        FROM `{TABELA_BASE}` b
        LEFT JOIN `{TABELA_EXEC}` e
          ON e.codigo_municipio_completo = b.codigo_municipio_completo
         AND e.rodada = %s
        WHERE b.url IS NOT NULL AND TRIM(b.url) <> ''
        GROUP BY b.regiao
        ORDER BY b.regiao
        """,
        (RODADA,),
    )
    observado = {r["regiao"]: int(r["total"] or 0) for r in rows}
    comparacao = []
    ok = True
    for regiao, esperado in DISTRIBUICAO_REGIONAL_ESPERADA.items():
        obs = observado.get(regiao, 0)
        igual = obs == esperado
        ok = ok and igual
        comparacao.append({"regiao": regiao, "esperado": esperado, "observado": obs, "situacao": situacao(igual)})
    # se aparecer região não esperada, reprova
    extras = sorted(set(observado) - set(DISTRIBUICAO_REGIONAL_ESPERADA))
    ok = ok and not extras
    resumo = "; ".join([f"{c['regiao']}: {c['observado']}/{c['esperado']}" for c in comparacao])
    return {"id": 4, "nome": "Preservação da distribuição regional da amostra", "ok": ok, "resumo": resumo, "dados": {"comparacao": comparacao, "regioes_extras": extras}}


def criterio_5_status_ferramentas(conn) -> Dict[str, Any]:
    total = q1(conn, f"SELECT COUNT(*) FROM `{TABELA_EXEC}` WHERE rodada = %s", (RODADA,)) or 0
    sem_status_ama = q1(conn, f"SELECT COUNT(*) FROM `{TABELA_EXEC}` WHERE rodada=%s AND (status_amaweb IS NULL OR TRIM(status_amaweb)='')", (RODADA,)) or 0
    sem_status_acc = q1(conn, f"SELECT COUNT(*) FROM `{TABELA_EXEC}` WHERE rodada=%s AND (status_accessmonitor IS NULL OR TRIM(status_accessmonitor)='')", (RODADA,)) or 0
    ok = total > 0 and sem_status_ama == 0 and sem_status_acc == 0
    return {
        "id": 5,
        "nome": "Registro de status por ferramenta",
        "ok": ok,
        "resumo": f"Sem status AMAWeb: {sem_status_ama}; sem status AccessMonitor: {sem_status_acc}; total de execuções: {total}.",
        "dados": {"total_execucoes": total, "sem_status_amaweb": sem_status_ama, "sem_status_accessmonitor": sem_status_acc},
    }


def criterio_6_validade_notas(conn) -> Dict[str, Any]:
    fora = qall(
        conn,
        f"""
        SELECT codigo_municipio_completo, nota_amaweb, nota_accessmonitor
        FROM `{TABELA_EXEC}`
        WHERE rodada = %s
          AND (
            (nota_amaweb IS NOT NULL AND (nota_amaweb < 0 OR nota_amaweb > 10))
            OR
            (nota_accessmonitor IS NOT NULL AND (nota_accessmonitor < 0 OR nota_accessmonitor > 10))
          )
        ORDER BY codigo_municipio_completo
        """,
        (RODADA,),
    )
    notas_ama = q1(conn, f"SELECT COUNT(*) FROM `{TABELA_EXEC}` WHERE rodada=%s AND nota_amaweb IS NOT NULL", (RODADA,)) or 0
    notas_acc = q1(conn, f"SELECT COUNT(*) FROM `{TABELA_EXEC}` WHERE rodada=%s AND nota_accessmonitor IS NOT NULL", (RODADA,)) or 0
    ok = len(fora) == 0
    return {"id": 6, "nome": "Validade das notas registradas", "ok": ok, "resumo": f"Notas AMAWeb preenchidas: {notas_ama}; notas AccessMonitor preenchidas: {notas_acc}; notas fora da faixa 0–10: {len(fora)}.", "dados": {"notas_amaweb": notas_ama, "notas_accessmonitor": notas_acc, "fora_faixa_total": len(fora), "fora_faixa": fora[:30]}}


def criterio_7_coerencia_flags(conn) -> Dict[str, Any]:
    incoer = qall(
        conn,
        f"""
        SELECT codigo_municipio_completo, nota_amaweb, tem_resultado_amaweb, nota_accessmonitor, tem_resultado_accessmonitor
        FROM `{TABELA_EXEC}`
        WHERE rodada = %s
          AND (
            (nota_amaweb IS NOT NULL AND COALESCE(tem_resultado_amaweb, 0) <> 1)
            OR (nota_amaweb IS NULL AND COALESCE(tem_resultado_amaweb, 0) = 1)
            OR (nota_accessmonitor IS NOT NULL AND COALESCE(tem_resultado_accessmonitor, 0) <> 1)
            OR (nota_accessmonitor IS NULL AND COALESCE(tem_resultado_accessmonitor, 0) = 1)
          )
        ORDER BY codigo_municipio_completo
        """,
        (RODADA,),
    )
    ok = len(incoer) == 0
    return {"id": 7, "nome": "Coerência entre nota e indicador de resultado", "ok": ok, "resumo": f"{len(incoer)} inconsistência(s) encontrada(s).", "dados": {"inconsistencias_total": len(incoer), "inconsistencias": incoer[:30]}}


def criterio_8_logs_minimos(conn) -> Dict[str, Any]:
    base_rows = qall(
        conn,
        f"SELECT codigo_municipio_completo FROM `{TABELA_BASE}` WHERE url IS NOT NULL AND TRIM(url) <> '' ORDER BY codigo_municipio_completo"
    )
    base_codes = [r["codigo_municipio_completo"] for r in base_rows]

    log_rows = qall(
        conn,
        f"""
        SELECT codigo_municipio_completo, ferramenta, COUNT(*) AS total
        FROM `{TABELA_LOG}`
        WHERE rodada = %s
          AND ferramenta IN ('amaweb', 'accessmonitor')
        GROUP BY codigo_municipio_completo, ferramenta
        """,
        (RODADA,),
    )
    log_map = {(r["codigo_municipio_completo"], r["ferramenta"]): int(r["total"] or 0) for r in log_rows}
    faltantes = []
    for code in base_codes:
        for ferr in FERRAMENTAS:
            if log_map.get((code, ferr), 0) < 1:
                faltantes.append({"codigo_municipio_completo": code, "ferramenta": ferr})

    ok = len(faltantes) == 0
    total_esperado = len(base_codes) * len(FERRAMENTAS)
    total_com_log = total_esperado - len(faltantes)
    return {"id": 8, "nome": "Rastreabilidade mínima das tentativas", "ok": ok, "resumo": f"{total_com_log}/{total_esperado} combinação(ões) município + ferramenta com ao menos um log.", "dados": {"esperado": total_esperado, "com_log": total_com_log, "faltantes_total": len(faltantes), "faltantes": faltantes[:50]}}


def _is_critical_log(row: Dict[str, Any]) -> bool:
    ferr = (row.get("ferramenta") or "").strip().lower()
    status = (row.get("status_tentativa") or "").strip().lower()
    msg = str(row.get("mensagem_erro") or "")
    tb = str(row.get("traceback_resumido") or "")
    combined = msg + "\n" + tb
    if ferr == "sistema":
        return True
    if status in CRITICAL_STATUS:
        return True
    for pat in CRITICAL_PATTERNS:
        if pat in combined:
            return True
    return False


def criterio_9_falhas_criticas(conn) -> Dict[str, Any]:
    total_base = q1(conn, f"SELECT COUNT(*) FROM `{TABELA_BASE}` WHERE url IS NOT NULL AND TRIM(url) <> ''") or 0
    logs = qall(
        conn,
        f"""
        SELECT codigo_municipio_completo, ferramenta, status_tentativa, mensagem_erro, traceback_resumido
        FROM `{TABELA_LOG}`
        WHERE rodada = %s
        """,
        (RODADA,),
    )

    critical = [r for r in logs if _is_critical_log(r)]
    por_ferr = {"amaweb": 0, "accessmonitor": 0, "sistema": 0, "outros": 0}
    exemplos = []
    seen = set()
    for r in critical:
        ferr = (r.get("ferramenta") or "outros").strip().lower()
        if ferr not in por_ferr:
            ferr = "outros"
        por_ferr[ferr] += 1
        key = (r.get("codigo_municipio_completo"), r.get("ferramenta"), r.get("status_tentativa"))
        if len(exemplos) < 20 and key not in seen:
            seen.add(key)
            exemplos.append({
                "codigo_municipio_completo": r.get("codigo_municipio_completo"),
                "ferramenta": r.get("ferramenta"),
                "status_tentativa": r.get("status_tentativa"),
                "mensagem_erro": str(r.get("mensagem_erro") or "")[:200],
            })

    # Critério: falhas críticas abaixo de 5% das execuções esperadas por ferramenta.
    # Eventos 'sistema' são avaliados contra o total esperado das duas ferramentas.
    den_tool = max(total_base, 1)
    den_sistema = max(total_base * len(FERRAMENTAS), 1)
    taxa_ama = por_ferr["amaweb"] / den_tool
    taxa_acc = por_ferr["accessmonitor"] / den_tool
    taxa_sistema = por_ferr["sistema"] / den_sistema
    ok = taxa_ama < LIMITE_FALHA_CRITICA and taxa_acc < LIMITE_FALHA_CRITICA and taxa_sistema < LIMITE_FALHA_CRITICA

    resumo = (
        f"Falhas críticas — AMAWeb: {por_ferr['amaweb']} ({fmt_pct(taxa_ama*100)}); "
        f"AccessMonitor: {por_ferr['accessmonitor']} ({fmt_pct(taxa_acc*100)}); "
        f"Sistema: {por_ferr['sistema']} ({fmt_pct(taxa_sistema*100)}). "
        f"Limite: < {fmt_pct(LIMITE_FALHA_CRITICA*100)}."
    )
    return {"id": 9, "nome": "Falhas críticas do sistema abaixo do limite admitido", "ok": ok, "resumo": resumo, "dados": {"total_base": total_base, "limite": LIMITE_FALHA_CRITICA, "por_ferramenta": por_ferr, "taxas": {"amaweb": taxa_ama, "accessmonitor": taxa_acc, "sistema": taxa_sistema}, "exemplos": exemplos}}


def criterio_10_viabilidade(conn) -> Dict[str, Any]:
    cols_base = set(table_columns(conn, TABELA_BASE))
    cols_exec = set(table_columns(conn, TABELA_EXEC))
    cols_log = set(table_columns(conn, TABELA_LOG))

    req_base = {"codigo_municipio_completo", "nome_municipio", "nome_uf", "regiao", "url"}
    req_exec = {
        "rodada", "codigo_municipio_completo",
        "nota_amaweb", "status_amaweb", "tem_resultado_amaweb",
        "nota_accessmonitor", "status_accessmonitor", "tem_resultado_accessmonitor",
        "status_geral",
    }
    req_log = {"rodada", "codigo_municipio_completo", "ferramenta", "tentativa", "status_tentativa"}

    faltam_base = sorted(req_base - cols_base)
    faltam_exec = sorted(req_exec - cols_exec)
    faltam_log = sorted(req_log - cols_log)

    total_exec = q1(conn, f"SELECT COUNT(*) FROM `{TABELA_EXEC}` WHERE rodada = %s", (RODADA,)) or 0
    pares_validos = q1(conn, f"SELECT COUNT(*) FROM `{TABELA_EXEC}` WHERE rodada=%s AND nota_amaweb IS NOT NULL AND nota_accessmonitor IS NOT NULL", (RODADA,)) or 0
    ama_validos = q1(conn, f"SELECT COUNT(*) FROM `{TABELA_EXEC}` WHERE rodada=%s AND nota_amaweb IS NOT NULL", (RODADA,)) or 0
    acc_validos = q1(conn, f"SELECT COUNT(*) FROM `{TABELA_EXEC}` WHERE rodada=%s AND nota_accessmonitor IS NOT NULL", (RODADA,)) or 0

    ok = not faltam_base and not faltam_exec and not faltam_log and total_exec > 0
    resumo = (
        f"Colunas obrigatórias ausentes — base: {len(faltam_base)}, execuções: {len(faltam_exec)}, logs: {len(faltam_log)}. "
        f"Observações válidas — AMAWeb: {ama_validos}, AccessMonitor: {acc_validos}, pares AMAWeb+AccessMonitor: {pares_validos}."
    )
    return {"id": 10, "nome": "Viabilidade dos cálculos previstos na análise", "ok": ok, "resumo": resumo, "dados": {"faltam_base": faltam_base, "faltam_execucoes": faltam_exec, "faltam_logs": faltam_log, "total_execucoes": total_exec, "amaweb_validos": ama_validos, "accessmonitor_validos": acc_validos, "pares_validos": pares_validos}}


def status_geral_execucoes(conn) -> List[Dict[str, Any]]:
    return qall(
        conn,
        f"""
        SELECT COALESCE(status_geral, 'sem_status') AS status_geral, COUNT(*) AS total
        FROM `{TABELA_EXEC}`
        WHERE rodada = %s
        GROUP BY COALESCE(status_geral, 'sem_status')
        ORDER BY total DESC, status_geral
        """,
        (RODADA,),
    )


def cobertura_ferramentas(conn) -> Dict[str, Any]:
    row = qall(
        conn,
        f"""
        SELECT
            COUNT(*) AS total_execucoes,
            SUM(CASE WHEN tem_resultado_amaweb = 1 THEN 1 ELSE 0 END) AS com_amaweb,
            SUM(CASE WHEN tem_resultado_accessmonitor = 1 THEN 1 ELSE 0 END) AS com_accessmonitor,
            SUM(CASE WHEN tem_resultado_amaweb = 1 AND tem_resultado_accessmonitor = 1 THEN 1 ELSE 0 END) AS com_ambas
        FROM `{TABELA_EXEC}`
        WHERE rodada = %s
        """,
        (RODADA,),
    )
    return row[0] if row else {}


# =====================
# Relatório
# =====================
def gerar_markdown(resultados: List[Dict[str, Any]], conn) -> str:
    aprovado = all(r["ok"] for r in resultados)
    parecer = "APROVADA" if aprovado else "REPROVADA"

    status_rows = status_geral_execucoes(conn)
    cob = cobertura_ferramentas(conn)

    eh_r00 = RODADA.lower() == "r00"

    if eh_r00:
        titulo_criterios = "Critérios de homologação"
        texto_objetivo = (
            "Este relatório registra a verificação objetiva da rodada técnica de homologação "
            f"`{RODADA}` da rotina de coleta automatizada do projeto. "
            "A rodada técnica não integra o conjunto analítico da pesquisa; sua finalidade é confirmar "
            "se a automação é capaz de produzir dados completos, consistentes e rastreáveis antes do início "
            "das rodadas oficiais `r01` a `r04`.\n"
        )
        texto_criterios = (
            "A rodada analisada foi avaliada por dez critérios obrigatórios de homologação. "
            "A verificação é considerada aprovada apenas quando todos os critérios são atendidos. "
            f"Para falhas críticas do sistema, adotou-se limite inferior a {fmt_pct(LIMITE_FALHA_CRITICA*100)} "
            "das execuções esperadas por ferramenta, em coerência com o erro amostral admitido no estudo.\n"
        )
        texto_parecer_aprovado = (
            f"A rodada técnica `{RODADA}` foi considerada **APROVADA**, pois todos os critérios obrigatórios "
            "de homologação foram atendidos. Com isso, a rotina de coleta automatizada pode ser considerada "
            "apta para a execução das rodadas oficiais, desde que preservados os mesmos parâmetros, estrutura "
            "de tabelas, fluxo de execução e forma de registro dos resultados.\n"
        )
        texto_parecer_reprovado = (
            f"A rodada técnica `{RODADA}` foi considerada **REPROVADA**, pois um ou mais critérios obrigatórios "
            "não foram atendidos. Antes do início das rodadas oficiais, recomenda-se corrigir a rotina operacional "
            "e repetir a homologação.\n"
        )
    else:
        titulo_criterios = "Critérios de conferência da rodada"
        texto_objetivo = (
            "Este relatório registra a conferência objetiva da rodada oficial "
            f"`{RODADA}` da rotina de coleta automatizada do projeto. "
            "Diferentemente da rodada técnica `r00`, as rodadas oficiais integram o conjunto analítico da pesquisa. "
            "A finalidade desta conferência é verificar, após a coleta, se os dados registrados permanecem completos, "
            "consistentes, rastreáveis e compatíveis com os critérios metodológicos definidos antes do início das "
            "rodadas oficiais.\n"
        )
        texto_criterios = (
            "A rodada analisada foi avaliada por dez critérios obrigatórios de conferência. "
            "A verificação é considerada aprovada apenas quando todos os critérios são atendidos. "
            f"Para falhas críticas do sistema, manteve-se o limite inferior a {fmt_pct(LIMITE_FALHA_CRITICA*100)} "
            "das execuções esperadas por ferramenta, conforme parâmetro definido na homologação operacional.\n"
        )
        texto_parecer_aprovado = (
            f"A rodada oficial `{RODADA}` foi considerada **APROVADA**, pois todos os critérios obrigatórios "
            "de conferência foram atendidos. Com isso, os dados da rodada podem ser mantidos no conjunto analítico "
            "da pesquisa, preservada sua identificação por rodada, ferramenta, município e status de coleta.\n"
        )
        texto_parecer_reprovado = (
            f"A rodada oficial `{RODADA}` foi considerada **REPROVADA**, pois um ou mais critérios obrigatórios "
            "não foram atendidos. Recomenda-se verificar os critérios reprovados antes de utilizar os dados desta "
            "rodada na análise consolidada.\n"
        )

    linhas = []
    linhas.append(f"# Anexo {ANEXO_LETRA} — Relatório de {TIPO_RELATORIO} da Rodada {RODADA}\n")
    linhas.append(f"**Data de geração:** {agora()}  ")
    linhas.append(f"**Tabelas consultadas:** `{TABELA_BASE}`, `{TABELA_EXEC}`, `{TABELA_LOG}`  ")
    linhas.append(f"**Parecer final:** **{parecer}**\n")

    linhas.append("## 1. Objetivo\n")
    linhas.append(texto_objetivo)

    linhas.append("## 2. Bases analisadas\n")
    linhas.append(
        "A verificação foi realizada diretamente no banco de dados operacional, considerando apenas os registros marcados com a rodada informada. "
        "A tabela de base amostral foi utilizada como referência para a cobertura esperada; a tabela de execuções foi utilizada para verificar resultados consolidados; e a tabela de logs foi utilizada para verificar rastreabilidade das tentativas e falhas críticas do sistema.\n"
    )

    linhas.append("## 3. Síntese operacional da rodada\n")
    linhas.append("### 3.1 Status geral das execuções\n")
    if status_rows:
        linhas.append(md_table(["Status geral", "Total"], [[r["status_geral"], r["total"]] for r in status_rows]) + "\n")
    else:
        linhas.append("Não foram encontrados registros de execução para a rodada analisada.\n")

    linhas.append("### 3.2 Cobertura por ferramenta\n")
    total_exec = int(cob.get("total_execucoes") or 0)
    com_ama = int(cob.get("com_amaweb") or 0)
    com_acc = int(cob.get("com_accessmonitor") or 0)
    com_ambas = int(cob.get("com_ambas") or 0)
    linhas.append(md_table(
        ["Indicador", "Total", "Percentual sobre execuções"],
        [
            ["Execuções consolidadas", total_exec, "100,00%" if total_exec else "0,00%"],
            ["Com nota AMAWeb", com_ama, fmt_pct(pct(com_ama, total_exec))],
            ["Com nota AccessMonitor", com_acc, fmt_pct(pct(com_acc, total_exec))],
            ["Com ambas as notas", com_ambas, fmt_pct(pct(com_ambas, total_exec))],
        ],
    ) + "\n")

    linhas.append(f"## 4. {titulo_criterios}\n")
    linhas.append(texto_criterios)

    for r in resultados:
        linhas.append(f"### 4.{r['id']} Critério {r['id']} — {r['nome']}\n")
        linhas.append(f"**Resultado:** {situacao(r['ok'])}.  ")
        linhas.append(f"**Síntese do cálculo:** {r['resumo']}\n")

        dados = r.get("dados") or {}
        if r["id"] == 1 and dados.get("faltantes_total"):
            linhas.append("Registros faltantes, limitados aos primeiros 30 casos:\n")
            linhas.append(md_table(["Código IBGE", "Município", "UF", "Região"], [[x.get("codigo_municipio_completo"), x.get("nome_municipio"), x.get("nome_uf"), x.get("regiao")] for x in dados.get("faltantes", [])]) + "\n")
        elif r["id"] == 4:
            linhas.append(md_table(["Região", "Esperado", "Observado", "Situação"], [[x["regiao"], x["esperado"], x["observado"], x["situacao"]] for x in dados.get("comparacao", [])]) + "\n")
        elif r["id"] == 9 and dados.get("exemplos"):
            linhas.append("Exemplos de falhas críticas identificadas, limitados aos primeiros 20 casos:\n")
            linhas.append(md_table(["Código IBGE", "Ferramenta", "Status", "Mensagem"], [[x.get("codigo_municipio_completo"), x.get("ferramenta"), x.get("status_tentativa"), x.get("mensagem_erro")] for x in dados.get("exemplos", [])]) + "\n")
        elif r["id"] == 10:
            linhas.append(md_table(
                ["Item", "Valor"],
                [
                    ["Colunas ausentes na base", ", ".join(dados.get("faltam_base", [])) or "Nenhuma"],
                    ["Colunas ausentes em execuções", ", ".join(dados.get("faltam_execucoes", [])) or "Nenhuma"],
                    ["Colunas ausentes em logs", ", ".join(dados.get("faltam_logs", [])) or "Nenhuma"],
                    ["Notas válidas AMAWeb", dados.get("amaweb_validos")],
                    ["Notas válidas AccessMonitor", dados.get("accessmonitor_validos")],
                    ["Pares válidos AMAWeb + AccessMonitor", dados.get("pares_validos")],
                ],
            ) + "\n")

    linhas.append("## 5. Síntese dos critérios\n")
    linhas.append(md_table(
        ["Critério", "Descrição", "Situação"],
        [[r["id"], r["nome"], situacao(r["ok"])] for r in resultados],
    ) + "\n")

    linhas.append("## 6. Parecer final\n")
    if aprovado:
        linhas.append(texto_parecer_aprovado)
    else:
        nao = [f"{r['id']} — {r['nome']}" for r in resultados if not r["ok"]]
        linhas.append(texto_parecer_reprovado)
        linhas.append("Critérios não atendidos:\n")
        linhas.append("\n".join([f"- {x}" for x in nao]) + "\n")

    return "\n".join(linhas)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = conectar()
    try:
        resultados = [
            criterio_1_cobertura(conn),
            criterio_2_unicidade(conn),
            criterio_3_vinculacao(conn),
            criterio_4_regional(conn),
            criterio_5_status_ferramentas(conn),
            criterio_6_validade_notas(conn),
            criterio_7_coerencia_flags(conn),
            criterio_8_logs_minimos(conn),
            criterio_9_falhas_criticas(conn),
            criterio_10_viabilidade(conn),
        ]
        aprovado = all(r["ok"] for r in resultados)
        md = gerar_markdown(resultados, conn)

        stamp = ts_nome()
        md_path = os.path.join(OUT_DIR, f"anexo_{ANEXO_SLUG}_conferencia_{RODADA}_{stamp}.md")
        latest_md = os.path.join(OUT_DIR, f"anexo_{ANEXO_SLUG}_conferencia_{RODADA}.md")
        json_path = os.path.join(OUT_DIR, f"anexo_{ANEXO_SLUG}_conferencia_{RODADA}_{stamp}.json")
        latest_json = os.path.join(OUT_DIR, f"anexo_{ANEXO_SLUG}_conferencia_{RODADA}.json")

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)
        with open(latest_md, "w", encoding="utf-8") as f:
            f.write(md)

        payload = {
            "rodada": RODADA,
            "anexo": ANEXO_LETRA,
            "tipo_relatorio": TIPO_RELATORIO,
            "gerado_em": agora(),
            "parecer": "APROVADA" if aprovado else "REPROVADA",
            "criterios": resultados,
            "tabelas": {"base": TABELA_BASE, "execucoes": TABELA_EXEC, "logs": TABELA_LOG},
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        with open(latest_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

        print(f"Relatório Markdown gerado: {md_path}")
        print(f"Resumo JSON gerado: {json_path}")
        print(f"Parecer final: {payload['parecer']}")

        if (not aprovado) and FALHAR_SE_REPROVADA:
            sys.exit(1)
        sys.exit(0)
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRO: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(2)
