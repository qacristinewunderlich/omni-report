# relatorios

Relatório único de velocity para múltiplas squads, com dados vindos do Jira e atualização automática por GitHub Actions.

## Objetivo

- Um único `index.html` para todas as squads.
- Troca de squad pelo filtro (`?squad=SAL`).
- Dados por squad em `data/{SQUAD}-data.json`.
- Métrica base: **Concluído** (issues com `statusCategory = done`), equivalente à coluna do relatório de velocity do Jira.
- A pasta `data/` inicia vazia (com `.gitkeep`) e será preenchida pelo script/Action.

## Organização e repositório

- Organização alvo: `OmniChat`
- Repositório alvo: `https://github.com/OmniChat/relatorios`
- Privacidade: **privado** (conforme decisão atual)

## Estrutura

```text
relatorios/
├── index.html
├── config/
│   └── squads.json
├── data/
│   └── <SQUAD>-data.json
├── scripts/
│   └── fetch-jira-data.py
└── .github/workflows/
    └── update-reports.yml
```

## Squads iniciais

Definidas em `config/squads.json`:

- AS (After Sales) - board 590
- BO (CloudOps) - board 133
- CP (Copilot) - board 216
- ECI (Outbound) - board 162
- IC (CRM) - board 955
- JNDS (Jornadas) - board 889
- LEAD (Inbound) - board 156
- MAN (Manager) - board 225
- SAL (Sales) - board 220
- SC (Commerce) - board 152
- SD (Dados) - board 260
- WC (Whizz Core) - board 219
- WR (Whizz Service) - board 218

## Secrets necessários no GitHub

Em `Settings > Secrets and variables > Actions`:

- `JIRA_SITE_URL` (ex: `https://omnichat.atlassian.net`)
- `JIRA_EMAIL`
- `JIRA_API_TOKEN` (nome sugerido do token: `github-actions-relatorios`)

## Execução local

### 1) Gerar JSONs manualmente

```bash
pip install requests
export JIRA_SITE_URL="https://omnichat.atlassian.net"
export JIRA_EMAIL="seu-email@empresa.com"
export JIRA_API_TOKEN="seu-token"
python scripts/fetch-jira-data.py --max-sprints 30
```

Para uma squad específica:

```bash
python scripts/fetch-jira-data.py --squad SAL --max-sprints 30
```

### 2) Abrir o relatório

```bash
python -m http.server 8080
```

Abrir: `http://localhost:8080/?squad=SAL`

## Workflow automático

Arquivo: `.github/workflows/update-reports.yml`

- Agendado: toda segunda-feira às 11:00 UTC (08:00 BRT).
- Manual: `Run workflow` com inputs opcionais:
  - `squad` (vazio = todas)
  - `max_sprints`

## Como adicionar nova squad

Adicionar um item em `config/squads.json`:

```json
{ "key": "NOVA", "name": "Nome Exibido", "boardId": 999, "projectKey": "NOVA" }
```

Sem outras mudanças: script e front já passam a considerar automaticamente.
