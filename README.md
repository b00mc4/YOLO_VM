# Village Guard Backend

Backend API ของระบบรักษาความปลอดภัยหมู่บ้านด้วยการอ่านป้ายทะเบียนรถจากกล้องวงจรปิด (YOLO + OCR) รับผลการตรวจจับจาก AI Vision แล้วบันทึก ตรวจกับ whitelist / blacklist แจ้งเตือนแบบ real-time ให้ดูภาพสดจากกล้อง และออกรายงาน รองรับหลายหมู่บ้านในระบบเดียว

พัฒนาด้วย FastAPI, PostgreSQL และ MediaMTX รันทั้งหมดด้วย Docker Compose

| เอกสาร | เนื้อหา |
| :--- | :--- |
| README.md (ไฟล์นี้) | ติดตั้ง อัปเดต ดูแลระบบ และแก้ปัญหาเบื้องต้น |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | สถาปัตยกรรม ฐานข้อมูล สิทธิ์ผู้ใช้ และ flow การทำงานภายใน |
| [.env.example](.env.example) | คำอธิบายตัวแปรตั้งค่าทุกตัว |
| `/docs` บนเซิร์ฟเวอร์ | Swagger: รายละเอียด request / response ของทุก endpoint |

---

## 1. ส่วนประกอบและพอร์ต

ระบบมี 4 service ใน `docker-compose.yml` อยู่ใน network `village_net` เดียวกัน

| Service | Container | พอร์ตบนเครื่อง host | หน้าที่ |
| :--- | :--- | :--- | :--- |
| `nginx` | `village_guard_nginx` | 80 | ทางเข้าเดียวของผู้ใช้ ส่งต่อไป frontend, api, MediaMTX และ AI Vision |
| `api` | `village_guard_api` | ไม่เปิด (8000 ภายใน) | FastAPI backend |
| `db` | `village_guard_db` | 5432 เฉพาะบนเครื่อง VM (`127.0.0.1`) | PostgreSQL 15 (ข้อมูลอยู่ใน volume `pgdata`) |
| `mediamtx` | `village_guard_mediamtx` | 8554 (RTSP), 8888 (HLS), 9997 (API เฉพาะบนเครื่อง VM `127.0.0.1`) | ดึงภาพจากกล้องแล้วส่งออกเป็น HLS ให้หน้าเว็บ รับเฉพาะการดูภาพ ไม่รับการส่งภาพเข้า (publish) |

nginx ส่ง request ต่อตาม path ดังนี้

| Path | ไปที่ |
| :--- | :--- |
| `/` | Frontend ที่ `192.168.100.97:3000` |
| `/smartlpr/` | AI Vision ที่ `192.168.100.97:8081` |
| `/api/`, `/docs`, `/openapi.json` | `api:8000` |
| `/api/sse/` | `api:8000` แบบปิด buffering (สำหรับ real-time) |
| `/mediamtx/` | `mediamtx:8888` (ภาพสด HLS) |

ระบบภายนอกที่ต้องมี: Frontend, AI Vision Service, SMTP server สำหรับส่งอีเมล และ proxy ที่จัดการ HTTPS ด้านหน้า nginx (nginx ฟังแค่พอร์ต 80)

> พอร์ต 5432 (ฐานข้อมูล) และ 9997 (MediaMTX API) เข้าได้เฉพาะจากบนเครื่อง VM เอง ส่วน 8554 และ 8888 ยังเปิดออกนอกเครื่อง ถ้าไม่มีระบบอื่นต้องใช้ ให้เอาออกจาก `ports:` ใน `docker-compose.yml` เพราะ firewall อย่าง ufw ปิดพอร์ตที่ Docker เปิดไว้ไม่ได้

---

## 2. สิ่งที่ต้องมีก่อนติดตั้ง

- เครื่อง Linux ที่ติดตั้ง Docker Engine และ Docker Compose v2 (ใช้คำสั่ง `docker compose`)
- `git` และ `openssl` (ใช้สร้าง key)
- บัญชี SMTP สำหรับส่งอีเมล ถ้าใช้ Gmail ต้องเปิด 2-Step Verification แล้วสร้าง App Password
- URL และ API key ของ AI Vision Service (ขอจากทีม AI Vision)
- โดเมนที่ชี้มาที่เครื่องนี้ พร้อม proxy ที่ทำ HTTPS

---

## 3. ติดตั้งครั้งแรก

**1) ดึงโค้ด**

```bash
git clone <repository-url> YOLO
cd YOLO
```

**2) สร้างไฟล์ `.env`**

```bash
cp .env.example .env
```

แก้ทุกค่าที่เป็น `change-me` และ `your-domain.example` คำอธิบายแต่ละตัวอยู่ใน `.env.example` ค่าลับให้สร้างใหม่ทุกครั้ง

```bash
openssl rand -hex 32                                       # ใช้กับ JWT_SECRET และ API_KEY (สร้างแยกกัน)
openssl ecparam -name prime256v1 -genkey -noout | base64 -w0  # ใช้กับ MEDIAMTX_JWT_PRIVATE_KEY_B64
```

**3) แก้ IP ใน `nginx/nginx.conf` ให้ตรงกับเครื่องจริง**

ไฟล์นี้เขียน IP ไว้ตายตัวสองจุด: Frontend `192.168.100.97:3000` (ใน `location /`) และ AI Vision `192.168.100.97:8081` (ใน `location ^~ /smartlpr/`)

**4) Build และเปิดระบบ**

```bash
docker compose up -d --build
docker compose ps        # ทุก service ต้องขึ้นสถานะ running
docker compose exec -u root api chown -R appuser:appgroup /app/storage
```

คำสั่งสุดท้ายให้ api เขียนรูปลง `storage/` ได้ เพราะ Docker สร้างโฟลเดอร์นี้เป็นของ root แต่ api รันด้วย `appuser` ที่ไม่ใช่ root

**5) สร้างตารางในฐานข้อมูล**

```bash
docker compose exec api alembic upgrade head
```

**6) สร้าง superadmin คนแรก**

```bash
docker compose exec api python create_superadmin.py \
  --username <username> \
  --email <email> \
  --fullname "<ชื่อ-นามสกุล>" \
  --password '<password>'
```

สคริปต์นี้ไม่ตรวจนโยบายรหัสผ่าน ให้ตั้งตามนโยบายของระบบเอง (8–36 ตัวอักษร มีตัวอักษรอังกฤษ ตัวเลข และสัญลักษณ์) และเนื่องจากรหัสผ่านจะติดอยู่ใน shell history ให้ลบออกหลังใช้ หรือเปลี่ยนรหัสผ่านหลัง login ครั้งแรก

**7) ตรวจว่าระบบทำงาน**

```bash
docker compose exec api python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read().decode())"
```

ต้องได้ `{"status":"ok"}` (`/health` ไม่ได้เปิดผ่าน nginx) จากนั้นเปิด `https://<โดเมน>/docs` ต้องเห็นหน้า Swagger แล้วลอง login ผ่านหน้าเว็บด้วยบัญชี superadmin

**8) แลกข้อมูลกับทีม AI Vision**

| ส่งให้ AI Vision | รับจาก AI Vision |
| :--- | :--- |
| ค่า `API_KEY` (AI ต้องส่งมาใน header `X-API-Key` ทุกครั้งที่ยิง webhook) | `AI_VISION_API_URL` และ `AI_VISION_API_KEY` |

ไม่ต้องส่ง URL ของ webhook ให้เอง ระบบส่ง `{BACKEND_PUBLIC_URL}/api/detections` ไปให้ AI Vision อัตโนมัติทุกครั้งที่เพิ่มกล้อง ถ้า AI Vision อยากทดสอบการเชื่อมต่อ ให้ส่ง `event_id` ที่ขึ้นต้นด้วย `TEST_Event_` ระบบจะตอบ `200` โดยไม่บันทึกข้อมูล

---

## 4. การอัปเดตระบบ

### อัตโนมัติ (GitHub Actions)

ทุกครั้งที่ push เข้า branch `main` workflow `.github/workflows/deploy.yml` จะรันบน self-hosted runner ที่ติดตั้งบนเครื่อง VM แล้วทำงานดังนี้

1. `cd /home/trainee/YOLO` แล้ว `git pull origin main`
2. `docker compose up -d --build --remove-orphans`
3. `docker compose restart nginx`

ถ้าย้ายเครื่องหรือย้ายโฟลเดอร์ ต้องแก้ path ใน `deploy.yml` และติดตั้ง runner ใหม่บนเครื่องปลายทาง

api รันจากโค้ดที่ build ไว้ใน image ไม่ได้อ่านไฟล์ในโฟลเดอร์โปรเจกต์ตรงๆ การแก้โค้ดบน VM จึงไม่มีผลจนกว่าจะ build ใหม่ ให้แก้ผ่าน git แล้ว deploy เท่านั้น

> **workflow นี้ไม่ได้รัน migration ให้** ทุกครั้งที่ update มีไฟล์ใหม่ใน `alembic/versions/` ต้องสั่งเองหลัง deploy เสร็จ
>
> ```bash
> docker compose exec api alembic upgrade head
> ```

### อัปเดตเอง

```bash
cd /home/trainee/YOLO
git pull origin main
docker compose up -d --build --remove-orphans
docker compose exec api alembic upgrade head
docker compose restart nginx
```

ก่อนอัปเดตที่มี migration ควร backup ฐานข้อมูลก่อนเสมอ (หัวข้อ 5)

---

## 5. งานดูแลประจำ

### ดู log และสถานะ

```bash
docker compose ps
docker compose logs -f api
docker compose logs --since 1h api
docker compose logs -f mediamtx
```

### แก้ `.env` แล้วต้องทำอะไร

`docker compose restart` **ไม่อ่าน `.env` ใหม่** ให้ใช้คำสั่งนี้แทน เพื่อสร้าง container ที่ค่าเปลี่ยนขึ้นใหม่

```bash
docker compose up -d
```

### Backup

เก็บไฟล์ไว้ที่ `~/backups` ห้ามเก็บในโฟลเดอร์โปรเจกต์ เพราะไฟล์จะถูก copy เข้า image ของ api ตอน build และต้อง backup ฐานข้อมูลกับรูปคู่กันเสมอ เพราะ CarTABLE เก็บ path ของรูปใน `storage/`

```bash
mkdir -p ~/backups
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > ~/backups/db_$(date +%F).dump
tar -cf ~/backups/storage_$(date +%F).tar --listed-incremental=$HOME/backups/storage.snar storage/
```

รูปใช้ backup แบบ incremental ครั้งแรกได้ไฟล์เต็ม ครั้งต่อไปเก็บเฉพาะรูปใหม่ ห้ามลบ `storage.snar` และห้ามกด Ctrl+C ระหว่าง tar ทำงาน

ตรวจไฟล์ (คำสั่งแรกต้องได้ 12 คำสั่งที่สองต้องขึ้น `STORAGE_OK`)

```bash
docker compose exec -T db pg_restore -l < ~/backups/db_$(date +%F).dump | grep -c "TABLE DATA"
tar -tf ~/backups/storage_$(date +%F).tar > /dev/null && echo STORAGE_OK
```

copy ออกนอก VM ทุกครั้ง (รันจากเครื่องที่ใช้เก็บสำเนา)

```bash
scp -p <user>@<ip-ของ-vm>:~/backups/* <โฟลเดอร์ปลายทาง>/
```

### Restore

```bash
docker compose stop api
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < ~/backups/db_YYYY-MM-DD.dump
for f in ~/backups/storage_*.tar; do sudo tar -xf "$f" --listed-incremental=/dev/null; done
docker compose start api
docker compose exec -u root api chown -R appuser:appgroup /app/storage
```

ให้รันในโฟลเดอร์โปรเจกต์ รูปที่ไม่มีใน backup ล่าสุดจะถูกลบ เพื่อให้ `storage/` ตรงกับฐานข้อมูล คำสั่งสุดท้ายคืนสิทธิ์ให้ api เขียนรูปใหม่ได้ ถ้ารูปมีจำนวนมากอาจใช้เวลาหลายนาที

### เข้าฐานข้อมูลหรือ MediaMTX API

ทั้งสองอย่างเปิดเฉพาะบนเครื่อง VM ถ้ารันคำสั่งบน VM ใช้ได้เลย

```bash
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
curl http://127.0.0.1:9997/v3/paths/list
```

ถ้าจะเปิดฐานข้อมูลจากเครื่องอื่น (เช่น DBeaver หรือ pgAdmin) ให้ต่อผ่าน SSH tunnel แล้วชี้โปรแกรมไปที่ `localhost:15432`

```bash
ssh -L 15432:127.0.0.1:5432 <user>@<ip-ของ-vm>
```

### พื้นที่ดิสก์

ระบบไม่ลบผลตรวจจับ รูป การแจ้งเตือน และ audit log อัตโนมัติ (ลบเฉพาะรูปกำพร้าที่ไม่มีข้อมูลในฐานข้อมูล) ควรเช็คพื้นที่เป็นระยะ

```bash
du -sh storage/
df -h
docker system df
```

### งานที่ระบบทำเองอัตโนมัติ

ไม่ต้องตั้ง cron เพิ่ม api มีงานเบื้องหลังในตัว เช่น เช็คสถานะกล้องทุก 10 วินาที, ลบ refresh token หมดอายุทุกชั่วโมง, ลบรูปกำพร้าทุกวัน รายการครบอยู่ใน [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) หัวข้อ 7

### ข้อควรรู้

- **api ต้องรันเป็น process เดียว** ห้ามเพิ่ม `--workers` หรือเพิ่มจำนวน container ของ api เพราะ session, rate limit, การล็อกบัญชี และ SSE เก็บอยู่ใน memory (รายละเอียดใน [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) หัวข้อ 8)
- **ทุกครั้งที่ restart api** การล็อกบัญชีจะถูกล้างและหน้าเว็บจะหลุดจาก real-time ชั่วคราว (เชื่อมต่อใหม่เอง) ผู้ใช้ไม่ต้อง login ใหม่

---

## 6. แก้ปัญหาเบื้องต้น

| อาการ | สาเหตุที่พบบ่อย | วิธีแก้ |
| :--- | :--- | :--- |
| api restart วนไม่หยุด, log มี `validation error for Settings` ... `Field required` | `.env` ขาดตัวแปร | เทียบ `.env` กับ `.env.example` ให้ครบ แล้ว `docker compose up -d` |
| log มี `cookie_samesite='none' requires cookie_secure=True` | ตั้ง `COOKIE_SAMESITE=none` แต่ `COOKIE_SECURE=false` | ตั้ง `COOKIE_SECURE=true` |
| login ได้ แต่สักพักโดนเด้งออก | refresh cookie ไม่ถูกเก็บ: `COOKIE_SECURE=true` แต่เปิดเว็บผ่าน http หรือ `COOKIE_DOMAIN` ไม่ตรงโดเมน | ใช้ HTTPS หรือแก้ค่า cookie ใน `.env` |
| ผู้ใช้ทุกคนโดน `429` พร้อมกัน | `TRUST_PROXY_HEADERS` / `TRUSTED_PROXY_HOPS` ไม่ตรงกับจำนวน proxy ระบบเลยเห็นทุกคนเป็น IP เดียวกัน | ตั้งตามคำอธิบายใน `.env.example` |
| ภาพสดไม่ขึ้น หรือกล้องขึ้น offline ทุกตัว | api คุยกับ MediaMTX ไม่ได้: `MEDIAMTX_API_URL` ไม่ใช่ `http://mediamtx:9997` (ใช้ IP ของ VM ไม่ได้ เพราะพอร์ต 9997 เปิดเฉพาะ `127.0.0.1`) | แก้ `.env` แล้ว `docker compose up -d` เมื่อ MediaMTX กลับมา ระบบจะลงทะเบียนกล้องใหม่เองภายใน 10 วินาที หรือสั่ง `POST /api/cameras/resync-all` |
| log มี `Startup camera resync gave up after 3 attempts` | MediaMTX ยังไม่พร้อมตอน api start | เช็ค `docker compose ps mediamtx` ปกติระบบจะ sync เองภายใน 10 วินาทีเมื่อ MediaMTX พร้อม |
| กล้องค้าง `pending` หรือเป็น `failed` | AI Vision ไม่ได้รับกล้อง หรืออ่านภาพจากกล้องไม่ได้ | เช็ค `AI_VISION_API_URL` / `AI_VISION_API_KEY` และ log ที่ขึ้นต้นด้วย `ai vision` แล้วสั่ง `POST /api/cameras/{id}/verification-check` |
| AI Vision ส่งผลมาแล้วได้ `401` | `X-API-Key` ไม่ตรงกับ `API_KEY` | ดู audit log action `api_key_rejected` แล้วเทียบ key กับทีม AI (ถ้าผิดเกิน 3 ครั้งใน 5 นาที จะได้ `429` ต้องรอ) |
| AI Vision ได้ `404` / `409` | `camera_id` ไม่มีในระบบ / กล้องหรือหมู่บ้านถูกปิด | เช็คกล้องในระบบ |
| บันทึกรูปไม่ได้ (`500`) | container รันด้วย user `appuser` แต่โฟลเดอร์ `storage/` เป็นของ root | `docker compose exec -u root api chown -R appuser:appgroup /app/storage` |
| อีเมลไม่ถูกส่ง | ค่า SMTP ผิด หรือใช้รหัสผ่านบัญชี Gmail แทน App Password | ดู `docker compose logs api` แล้วแก้ค่า `SMTP_*` (ระหว่างที่ SMTP ล่ม ระบบจะข้ามการส่งอีเมลแจ้งเตือน blacklist) |
| ลิงก์ในอีเมลเปิดไม่ได้ | `FRONTEND_URL` ผิด | แก้ให้เป็น URL ของหน้าเว็บ ไม่มี `/` ปิดท้าย |
| ผู้ใช้แจ้งว่าบัญชีถูกล็อก (login ได้ `423`) | ใส่รหัสผิดหลายครั้ง | admin ดู `GET /api/users/locked-accounts` แล้วปลดด้วย `POST /api/users/{id}/unlock-account` หรือรอให้หมดเวลา |
| ส่งภาพเข้า MediaMTX ด้วย ffmpeg หรือ OBS ไม่ได้ (`401`) | ระบบตั้งใจปฏิเสธการ publish ทุกกรณี (api log มี `denied action=publish`) | ถ้าจำเป็นต้อง publish จริง ต้องเพิ่มเงื่อนไขใน `app/api/endpoints/mediamtx.py` |
| แก้โค้ดบน VM แล้วไม่มีผล | api รันจากโค้ดใน image | แก้ผ่าน git แล้ว `docker compose up -d --build` |
| หน้า real-time ไม่อัปเดต | proxy ด้านหน้า buffer ข้อมูล SSE ไว้ | `curl -N https://<โดเมน>/api/sse/test` ต้องเห็นข้อความทยอยมาทีละวินาที 5 ข้อความ ถ้ามาพร้อมกันทีเดียวให้ปิด buffering ที่ proxy |

---

## 7. สำหรับนักพัฒนา

รัน api บนเครื่องโดยไม่ใช้ Docker (ต้องมี PostgreSQL และ MediaMTX ที่เข้าถึงได้)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # ตั้ง DATABASE_URL และ MEDIAMTX_API_URL ให้ชี้ localhost
alembic upgrade head
uvicorn app.main:app --reload
```

- ตรวจรูปแบบโค้ด: `ruff check .`
- แก้ model แล้วสร้าง migration: `alembic revision --autogenerate -m "คำอธิบาย"` จากนั้นเปิดตรวจไฟล์ใน `alembic/versions/` ทุกครั้ง ให้สร้างบนเครื่องนักพัฒนาแล้ว commit อย่าสร้างผ่าน `docker compose exec api` เพราะไฟล์จะอยู่แค่ใน container และหายเมื่อ build ใหม่
- โครงสร้างโค้ดและ flow ภายใน: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## 8. ทีมผู้พัฒนา

| ส่วนงาน | ผู้รับผิดชอบ | ติดต่อ |
| :--- | :--- | :--- |
| Backend | `<ชื่อ>` | `<อีเมล>` |
| Frontend | `<ชื่อ>` | `<อีเมล>` |
| AI Vision | `<ชื่อ>` | `<อีเมล>` |
