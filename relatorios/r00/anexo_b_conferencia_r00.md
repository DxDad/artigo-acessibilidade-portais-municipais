# Anexo B — Relatório de Homologação da Rodada r00

**Data de geração:** 2026-06-22 23:08:28  
**Tabelas consultadas:** `tcc_unifal_base_amostral`, `tcc_unifal_execucoes`, `tcc_unifal_logs_validacao`  
**Parecer final:** **APROVADA**

## 1. Objetivo

Este relatório registra a verificação objetiva da rodada técnica de homologação `r00` da rotina de coleta automatizada do projeto. A rodada técnica não integra o conjunto analítico da pesquisa; sua finalidade é confirmar se a automação é capaz de produzir dados completos, consistentes e rastreáveis antes do início das rodadas oficiais `r01` a `r04`.

## 2. Bases analisadas

A verificação foi realizada diretamente no banco de dados operacional, considerando apenas os registros marcados com a rodada informada. A tabela de base amostral foi utilizada como referência para a cobertura esperada; a tabela de execuções foi utilizada para verificar resultados consolidados; e a tabela de logs foi utilizada para verificar rastreabilidade das tentativas e falhas críticas do sistema.

## 3. Síntese operacional da rodada

### 3.1 Status geral das execuções

| Status geral | Total |
| --- | --- |
| completo | 350 |
| parcial | 9 |
| falha_total | 3 |

### 3.2 Cobertura por ferramenta

| Indicador | Total | Percentual sobre execuções |
| --- | --- | --- |
| Execuções consolidadas | 362 | 100,00% |
| Com nota AMAWeb | 355 | 98,07% |
| Com nota AccessMonitor | 354 | 97,79% |
| Com ambas as notas | 350 | 96,69% |

## 4. Critérios de homologação

A rodada analisada foi avaliada por dez critérios obrigatórios de homologação. A verificação é considerada aprovada apenas quando todos os critérios são atendidos. Para falhas críticas do sistema, adotou-se limite inferior a 5,00% das execuções esperadas por ferramenta, em coerência com o erro amostral admitido no estudo.

### 4.1 Critério 1 — Cobertura da base amostral

**Resultado:** Atendido.  
**Síntese do cálculo:** 362/362 municípios com execução (100,00%).

### 4.2 Critério 2 — Unicidade dos registros por município e rodada

**Resultado:** Atendido.  
**Síntese do cálculo:** 0 duplicidade(s) encontrada(s).

### 4.3 Critério 3 — Vinculação entre execução e base amostral

**Resultado:** Atendido.  
**Síntese do cálculo:** 0 registro(s) de execução sem correspondência na base, em 362 execução(ões).

### 4.4 Critério 4 — Preservação da distribuição regional da amostra

**Resultado:** Atendido.  
**Síntese do cálculo:** Centro-Oeste: 31/31; Nordeste: 116/116; Norte: 30/30; Sudeste: 108/108; Sul: 77/77

| Região | Esperado | Observado | Situação |
| --- | --- | --- | --- |
| Centro-Oeste | 31 | 31 | Atendido |
| Nordeste | 116 | 116 | Atendido |
| Norte | 30 | 30 | Atendido |
| Sudeste | 108 | 108 | Atendido |
| Sul | 77 | 77 | Atendido |

### 4.5 Critério 5 — Registro de status por ferramenta

**Resultado:** Atendido.  
**Síntese do cálculo:** Sem status AMAWeb: 0; sem status AccessMonitor: 0; total de execuções: 362.

### 4.6 Critério 6 — Validade das notas registradas

**Resultado:** Atendido.  
**Síntese do cálculo:** Notas AMAWeb preenchidas: 355; notas AccessMonitor preenchidas: 354; notas fora da faixa 0–10: 0.

### 4.7 Critério 7 — Coerência entre nota e indicador de resultado

**Resultado:** Atendido.  
**Síntese do cálculo:** 0 inconsistência(s) encontrada(s).

### 4.8 Critério 8 — Rastreabilidade mínima das tentativas

**Resultado:** Atendido.  
**Síntese do cálculo:** 724/724 combinação(ões) município + ferramenta com ao menos um log.

### 4.9 Critério 9 — Falhas críticas do sistema abaixo do limite admitido

**Resultado:** Atendido.  
**Síntese do cálculo:** Falhas críticas — AMAWeb: 0 (0,00%); AccessMonitor: 4 (1,10%); Sistema: 0 (0,00%). Limite: < 5,00%.

Exemplos de falhas críticas identificadas, limitados aos primeiros 20 casos:

| Código IBGE | Ferramenta | Status | Mensagem |
| --- | --- | --- | --- |
| 4300638 | accessmonitor | erro | erro |
| 3555604 | accessmonitor | erro | erro |

### 4.10 Critério 10 — Viabilidade dos cálculos previstos na análise

**Resultado:** Atendido.  
**Síntese do cálculo:** Colunas obrigatórias ausentes — base: 0, execuções: 0, logs: 0. Observações válidas — AMAWeb: 355, AccessMonitor: 354, pares AMAWeb+AccessMonitor: 350.

| Item | Valor |
| --- | --- |
| Colunas ausentes na base | Nenhuma |
| Colunas ausentes em execuções | Nenhuma |
| Colunas ausentes em logs | Nenhuma |
| Notas válidas AMAWeb | 355 |
| Notas válidas AccessMonitor | 354 |
| Pares válidos AMAWeb + AccessMonitor | 350 |

## 5. Síntese dos critérios

| Critério | Descrição | Situação |
| --- | --- | --- |
| 1 | Cobertura da base amostral | Atendido |
| 2 | Unicidade dos registros por município e rodada | Atendido |
| 3 | Vinculação entre execução e base amostral | Atendido |
| 4 | Preservação da distribuição regional da amostra | Atendido |
| 5 | Registro de status por ferramenta | Atendido |
| 6 | Validade das notas registradas | Atendido |
| 7 | Coerência entre nota e indicador de resultado | Atendido |
| 8 | Rastreabilidade mínima das tentativas | Atendido |
| 9 | Falhas críticas do sistema abaixo do limite admitido | Atendido |
| 10 | Viabilidade dos cálculos previstos na análise | Atendido |

## 6. Parecer final

A rodada técnica `r00` foi considerada **APROVADA**, pois todos os critérios obrigatórios de homologação foram atendidos. Com isso, a rotina de coleta automatizada pode ser considerada apta para a execução das rodadas oficiais, desde que preservados os mesmos parâmetros, estrutura de tabelas, fluxo de execução e forma de registro dos resultados.
