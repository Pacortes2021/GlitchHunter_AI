# GlitchHunter 24/7 gratis en Oracle Cloud (región Santiago)

Con esta guía, el radar queda corriendo todo el día en una máquina gratuita de Oracle Cloud ("Always Free").
Se reinicia solo si se cae y respalda la base de datos todos los días.

Tiempo estimado: unos 30 minutos.

---

## 1. Crear la cuenta

1. Entra a <https://www.oracle.com/cloud/free/> y crea la cuenta.
2. Te pedirá una tarjeta solo para verificar tu identidad. Si usas únicamente recursos *Always Free*, no se cobra nada.
3. **Región principal (Home Region): elige `Chile Central (Santiago)`.**
   - Esto es importante y **no se puede cambiar después**, porque los recursos gratis solo existen en tu región principal.
   - Con una región en Chile, las tiendas ven una IP chilena y responden más rápido.

## 2. Crear tu llave SSH (en tu Mac)

Si ya tienes `~/.ssh/id_ed25519.pub`, sáltate este paso.

```bash
ssh-keygen -t ed25519 -C "glitchhunter"
```

Presiona Enter en todas las preguntas. Luego copia tu llave pública:

```bash
pbcopy < ~/.ssh/id_ed25519.pub
```

## 3. Crear la máquina

En la consola de Oracle: **☰ → Compute → Instances → Create instance**.

| Campo | Valor |
|---|---|
| Name | `glitchhunter` |
| Image | **Canonical Ubuntu 24.04** (botón *Change image*) |
| Shape | **Ampere → VM.Standard.A1.Flex**. Marca 2 OCPU y 12 GB, o lo máximo que muestre la etiqueta *Always Free-eligible* |
| Networking | Deja la red que propone, con **"Assign a public IPv4 address"** activado |
| Add SSH keys | **Paste public keys** → pega lo que copiaste en el paso 2 |
| Boot volume | Deja el tamaño por defecto (el plan gratis cubre hasta 200 GB) |

Presiona **Create**. En 1–2 minutos queda en estado *Running*. Copia la **Public IP address**.

> **"Out of capacity"**: a veces no quedan máquinas ARM gratis en ese momento. Prueba con otro *Availability domain* (si aparece) o reintenta más tarde. Suele liberarse en horas.

> **No abras puertos.** No hace falta tocar las *Security Lists* ni abrir el 8080: el panel se ve por túnel (paso 6) y así nadie más puede entrar.

## 4. Instalar el radar

Desde tu Mac, conéctate a la máquina. Cambia `IP` por la IP pública:

```bash
ssh ubuntu@IP
```

Ya dentro de la máquina, ejecuta el instalador:

```bash
curl -fsSL https://raw.githubusercontent.com/Pacortes2021/GlitchHunter_AI/main/deploy/install.sh | sudo bash
```

El instalador hace lo siguiente:

- Instala Python y las dependencias.
- Descarga el radar en `/opt/glitchhunter`.
- Crea la configuración `/opt/glitchhunter/.env`.
- Lo deja como servicio que arranca solo.
- Programa un respaldo diario de la base de datos.

Al final muestra **"Radar funcionando ✅"** y una **clave del panel**. Guárdala: el panel la pide solo para "Escanear ahora", simular ofertas o editar la watchlist.

## 5. (Opcional) Llevar tu base de datos actual

Así no partes de cero: llevas el historial de precios y el canal con las publicaciones de hoy.

**En tu Mac**, haz una copia consistente y súbela:

```bash
cd ~/Desktop/GlitchHunter_AI/glitchhunter_ai/data
```

```bash
sqlite3 glitchhunter.db ".backup /tmp/glitchhunter-subir.db"
```

```bash
scp /tmp/glitchhunter-subir.db ubuntu@IP:/tmp/
```

**En la máquina**, reemplaza la base vacía:

```bash
sudo systemctl stop glitchhunter
```

```bash
sudo mv /tmp/glitchhunter-subir.db /var/lib/glitchhunter/glitchhunter.db
```

```bash
sudo rm -f /var/lib/glitchhunter/glitchhunter.db-wal /var/lib/glitchhunter/glitchhunter.db-shm
```

```bash
sudo chown glitchhunter:glitchhunter /var/lib/glitchhunter/glitchhunter.db
```

```bash
sudo systemctl start glitchhunter
```

## 6. Ver el panel

El radar solo escucha dentro de la máquina (`127.0.0.1:8080`). Hay dos formas de abrirlo.

### Opción A: túnel SSH (desde tu Mac, sin instalar nada)

```bash
ssh -N -L 8080:127.0.0.1:8080 ubuntu@IP
```

Mientras ese comando esté abierto, entra a <http://localhost:8080> en tu navegador.

> Si tu radar local del Mac sigue corriendo en el 8080, usa otro puerto local: `-L 8081:127.0.0.1:8080` y abre <http://localhost:8081>.

### Opción B: Tailscale (desde el celular y cualquier equipo tuyo)

Tailscale crea una red privada entre tus dispositivos. Es gratis para uso personal.

1. Crea una cuenta en <https://tailscale.com> e instala la app en tu celular y en tu Mac.
2. En la máquina, instala Tailscale:

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   ```

3. Conecta la máquina a tu cuenta. Abre el link que muestra y entra con tu cuenta:

   ```bash
   sudo tailscale up
   ```

4. Publica el panel solo dentro de tu red privada:

   ```bash
   sudo tailscale serve --bg 8080
   ```

5. El comando muestra una dirección del tipo `https://glitchhunter.tu-red.ts.net`. Agrégala como origen permitido:

   ```bash
   sudo nano /opt/glitchhunter/.env
   ```

   Agrega o edita esta línea, con tu dirección. Guarda con Ctrl+O, Enter y Ctrl+X:

   ```
   CORS_ORIGINS=https://glitchhunter.tu-red.ts.net,http://localhost:8080,http://127.0.0.1:8080
   ```

   ```bash
   sudo systemctl restart glitchhunter
   ```

6. Abre esa dirección desde el celular, con la app de Tailscale conectada. Solo tus dispositivos pueden verla.

## 7. Uso diario

| Para… | Comando (en la máquina) |
|---|---|
| Ver el registro en vivo | `sudo journalctl -u glitchhunter -f` |
| Reiniciar | `sudo systemctl restart glitchhunter` |
| Detener | `sudo systemctl stop glitchhunter` |
| Actualizar a la última versión | `sudo bash /opt/glitchhunter/deploy/install.sh` |
| Respaldar ahora | `sudo -u glitchhunter /usr/local/bin/glitchhunter-backup` |
| Ver respaldos | `ls -lh /var/lib/glitchhunter/respaldos` |
| Cambiar la configuración | `sudo nano /opt/glitchhunter/.env` y luego reiniciar |

Cuando el radar ya funcione en la nube, **apaga el del Mac**. Si quedan los dos encendidos, cada tienda recibe el doble de visitas, y eso aumenta el riesgo de bloqueos.

## 8. Si algo falla

- **Una tienda deja de aportar ofertas.** Las IPs de centros de datos se bloquean más que las de casa. Revisa *Estado → Salud de las tiendas* en el panel, o `sudo journalctl -u glitchhunter | grep "bloqueó"`. El radar pausa sola la tienda bloqueada y la retoma después; no hay que hacer nada.
- **Tiendas que bloquean siempre la IP del servidor** (p. ej. Ripley, Paris, Sodimac). Repártelas con el PC de la casa:
  - en el servidor, `/opt/glitchhunter/.env`: `RADAR_SKIP_STORES=ripley,paris,sodimac` y reinicia;
  - en el PC, su `.env`: `RADAR_ONLY_STORES=ripley,paris,sodimac`.
  Así cada tienda la lee un solo radar y no recibe visitas dobles.
- **El servicio no arranca.** Revisa el error con `sudo journalctl -u glitchhunter -n 100`.
- **Oracle detiene la máquina por "inactiva".** Oracle puede recuperar máquinas gratis que pasan 7 días casi sin uso. El radar usa CPU y red todo el día, así que no debería pasar. Si pasa, enciéndela desde la consola. Pasar la cuenta a *Pay As You Go* lo evita, y los recursos gratis siguen sin costo; si lo haces, crea un *Budget* con alerta en $1 para enterarte de cualquier cobro.
- **Volver a empezar.** Vuelve a correr el instalador; no borra la base de datos ni la configuración.
