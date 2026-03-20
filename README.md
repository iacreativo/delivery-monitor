# Job Tracker (Delivery Monitor)

Microservicio que monitorea deliveries pendientes en Supabase y marca como `failed` aquellos que no reciben imagen en la galería personal después de 8 minutos.

## Funcionalidad

1. **Revisa tabla `deliveries`** en Supabase cada 1 minuto
2. **Busca imágenes en `gallery_personal`** (via storage-proxy)
3. **Si no llega imagen después de 8 minutos:**
   - Marca delivery como `failed` en Supabase
   - Registra error en `gallery_errors`
   - Reembolsa créditos automáticamente

## Configuración

| Variable | Default | Descripción |
|---------|---------|-------------|
| `SUPABASE_URL` | - | URL del proyecto Supabase |
| `SUPABASE_SERVICE_ROLE_KEY` | - | Service role key (para escribir en BD) |
| `STORAGE_PROXY_URL` | https://storage.vyzo.cloud | URL del storage proxy |
| `DELIVERY_TIMEOUT_MINUTES` | 8 | Minutos antes de marcar como failed |
| `CHECK_INTERVAL_MINUTES` | 1 | Frecuencia de verificación |

## Despliegue en Coolify

### 1. Crear aplicación

En Coolify, crear nueva aplicación → Docker based

### 2. Configurar

- **Repository:** https://github.com/iacreativo/delivery-monitor
- **Branch:** main
- **Port:** 5001

### 3. Variables de entorno

```bash
SUPABASE_URL=https://bjytqumpnilipanxhuic.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<tu-service-role-key>
STORAGE_PROXY_URL=https://storage.vyzo.cloud
DELIVERY_TIMEOUT_MINUTES=8
CHECK_INTERVAL_MINUTES=1
```

### 4. Health check

```bash
curl https://job-tracker.vyzo.cloud/health
```

Respuesta esperada:
```json
{
  "status": "ok",
  "pending": 0,
  "timeout": 8
}
```

## API Endpoints

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/health` | GET | Estado del servicio |
| `/reset` | POST | Limpiar lista de pendientes (mantiene en memoria) |

## Tabla deliveries (Supabase)

```sql
CREATE TABLE deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    credits_used INTEGER NOT NULL,
    status VARCHAR(50) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

## Tabla gallery_errors (PostgreSQL/storage-proxy)

```sql
CREATE TABLE gallery_errors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255) NOT NULL,
    error_message TEXT NOT NULL,
    execution_id VARCHAR(255),
    refund_credits BOOLEAN DEFAULT TRUE,
    credits_amount INTEGER DEFAULT 2,
    timestamp TIMESTAMPTZ DEFAULT NOW()
);
```

## Logs

El servicio imprime logs claros:

```
[Monitor] Tracking delivery xxx (status=pending)
[Monitor] Checking delivery xxx for user yyy (5.2 min ago)
[Monitor] ✓ Delivery xxx: Found image in gallery
[Monitor] ✗ Delivery xxx: NO image found - FAILING
[Monitor] ✓ Refunded 2 credits for xxx
```

## Notas

- El servicio usa **memoria** para mantener pendientes entre verificaciones
- Si el servicio se reinicia, resincroniza con Supabase al arrancar
- El `/reset` solo limpia la memoria, no afecta a Supabase
