# Deploy do Vende Fácil

## O detalhe importante

O app usa SQLite (`vende_facil.sqlite`) e também salva imagens importadas em `data/uploads/`. Em servidor, configure um disco/volume persistente apontando para a pasta `data` ou defina `VENDE_FACIL_DATA_DIR` para a pasta persistente do provedor.

Sem volume persistente, o app pode funcionar, mas seus dados e imagens importadas podem sumir em redeploy/restart.

## Variáveis de ambiente

Obrigatórias/recomendadas:

```env
VENDE_FACIL_LOGIN_EMAIL=admin@vendefacil.com
VENDE_FACIL_LOGIN_PASSWORD=troque-essa-senha
VENDE_FACIL_SECRET=uma-chave-grande-e-aleatoria
VENDE_FACIL_DATA_DIR=./data
```

## Render

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
```

Health check path:

```txt
/health
```

Para persistir o SQLite, adicione um Disk e use como mount path:

```txt
/opt/render/project/src/data
```

Depois defina:

```env
VENDE_FACIL_DATA_DIR=/opt/render/project/src/data
```

## Railway

O projeto já inclui `railway.toml`.

Para persistir o SQLite, adicione um Volume no serviço e monte em:

```txt
/app/data
```

Depois defina:

```env
VENDE_FACIL_DATA_DIR=/app/data
```

## Docker/VPS

Build:

```bash
docker build -t vende-facil .
```

Run:

```bash
docker run -p 5433:5433 \
  -e PORT=5433 \
  -e VENDE_FACIL_LOGIN_EMAIL=admin@vendefacil.com \
  -e VENDE_FACIL_LOGIN_PASSWORD='troque-essa-senha' \
  -e VENDE_FACIL_SECRET='uma-chave-grande' \
  -e VENDE_FACIL_DATA_DIR=/app/data \
  -v vende_facil_data:/app/data \
  vende-facil
```
