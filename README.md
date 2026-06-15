# EB104 Lab — Site comercial

Site de prezentare pentru **DMAECS-EB104-lab**: monitorizare energetică, automatizări IoT, tablouri electrice inteligente și infrastructură locală.

Construit cu **Astro** (static, fără backend, fără dependențe cloud obligatorii).

---

## Structură proiect

```
sitee/
├── assets/
│   ├── favicon.svg
│   └── images/
│       ├── tablou-electric.jpg          ← folosit în Hero
│       ├── monitorizare-tablou-electric.png  ← folosit în Demo Project
│       └── monitorizare putere.png      ← folosit în Demo Project
├── src/
│   ├── components/
│   │   ├── Header.astro
│   │   ├── Hero.astro
│   │   ├── Services.astro
│   │   ├── Solutions.astro
│   │   ├── DemoProject.astro
│   │   ├── ForWho.astro
│   │   ├── TechStack.astro
│   │   ├── CTA.astro
│   │   └── Footer.astro
│   ├── layouts/
│   │   └── Layout.astro
│   └── pages/
│       └── index.astro
├── astro.config.mjs
├── package.json
├── tsconfig.json
└── README.md
```

> **Notă:** Folderul `assets/` este configurat ca `publicDir` în Astro.
> Imaginile din `assets/images/` sunt servite direct la `/images/...`.

---

## Rulare locală

### Cerințe
- **Node.js** versiunea 18 sau mai nouă  
  Verificare: `node -v`  
  Download: https://nodejs.org

### Pași

```bash
# 1. Intră în folderul proiectului
cd sitee

# 2. Instalează dependențele (prima dată)
npm install

# 3. Pornește serverul de dezvoltare
npm run dev
```

Site-ul va fi disponibil la: **http://localhost:4321**

Modificările în fișierele `.astro` se reflectă automat în browser (hot reload).

---

## Build pentru producție

```bash
npm run build
```

Rezultatul se generează în folderul **`dist/`**.

Preview local al build-ului:
```bash
npm run preview
```

---

## Deploy pe server cu Nginx

### 1. Build local

```bash
npm run build
```

### 2. Copiază folderul `dist/` pe server

```bash
scp -r dist/ user@server.tău:/var/www/eb104lab/
```

sau cu rsync:
```bash
rsync -avz --delete dist/ user@server.tău:/var/www/eb104lab/
```

### 3. Configurare Nginx

```nginx
server {
    listen 80;
    server_name eb104lab.ro www.eb104lab.ro;

    root /var/www/eb104lab;
    index index.html;

    location / {
        try_files $uri $uri/ $uri.html =404;
    }

    # Cache static assets
    location ~* \.(jpg|jpeg|png|gif|svg|ico|woff2?)$ {
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    # Gzip
    gzip on;
    gzip_types text/plain text/css application/javascript image/svg+xml;
}
```

Reîncarcă Nginx:
```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 4. HTTPS cu Cloudflare Tunnel (recomandat)

Nu ai nevoie de IP public sau certificat manual:

```bash
# Instalează cloudflared pe server
# https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/

cloudflared tunnel create eb104lab
cloudflared tunnel route dns eb104lab eb104lab.ro
cloudflared tunnel run eb104lab
```

Sau configurează ca serviciu systemd pentru pornire automată.

---

## Urcarea pe GitHub

```bash
# Init repository (dacă nu există deja)
git init
git add .
git commit -m "Initial commit: EB104 Lab site"

# Conectează la un repo GitHub nou
git remote add origin https://github.com/USER/eb104-lab.git
git branch -M main
git push -u origin main
```

> **Notă:** Adaugă un `.gitignore` înainte de primul commit:

```bash
echo "node_modules/\ndist/\n.astro/" > .gitignore
```

---

## Personalizare

| Element | Fișier |
|---|---|
| Culori și fonturi | `src/layouts/Layout.astro` → `:root {}` |
| Meniu și logo | `src/components/Header.astro` |
| Secțiunea Hero | `src/components/Hero.astro` |
| Servicii (carduri) | `src/components/Services.astro` |
| Fluxul tehnic | `src/components/Solutions.astro` |
| Proiect demo + imagini | `src/components/DemoProject.astro` |
| Pentru cine | `src/components/ForWho.astro` |
| Stack tehnic | `src/components/TechStack.astro` |
| Contact (CTA) | `src/components/CTA.astro` |
| Footer (contact, linkuri) | `src/components/Footer.astro` |

### Actualizare date contact

În `src/components/CTA.astro` și `src/components/Footer.astro`:
- Înlocuiește `contact@eb104lab.ro` cu emailul real
- Înlocuiește `+40 XXX XXX XXX` cu numărul real

---

## Ce folder servește Nginx

```
dist/
```

Acesta este generat de `npm run build` și conține tot HTML-ul, CSS-ul, JavaScript-ul și imaginile statice optimizate.
