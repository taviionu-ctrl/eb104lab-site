const sharp = require('sharp');
const path = require('path');
// Sursele (fotografii brute) stau în afara folderului publicat
const srcDir = path.join(__dirname, '..', '_source-photos') + path.sep;
// Rezultatele optimizate (WebP) merg în folderul servit
const outDir = path.join(__dirname, '..', 'assets', 'images') + path.sep;

// color grade comun: contrast usor, saturatie+, fara tenta (whites neutri)
const grade = (img) => img
  .modulate({ saturation: 1.11, brightness: 1.01 })
  .linear(1.06, -6)
  .gamma(1.04)
  .sharpen({ sigma: 0.6 });

const jobs = [
  // HERO — tablou vertical: crop tubulatura/perete sus + gaura/firele de jos
  { src: '452c87c5-f927-40ce-86fd-94a1e0f05e9b', out: 'hero-tablou.webp',
    extract: { left: 0, top: 300, width: 934, height: 1270 } },
  // PROIECT DEMO — banner landscape: trim margini + taie firele de jos
  { src: 'd2e743cc-8aa2-4d28-a86c-74f1d7ed6016', out: 'tablou-banner.webp',
    extract: { left: 150, top: 8, width: 1700, height: 800 } },
  // PROIECT DEMO — mini-analizor de retea (1 circuit), enclosure ABB
  { src: '9932bbf4-850b-4d08-9895-26b61b35de98', out: 'mini-analizor.webp',
    extract: { left: 70, top: 500, width: 860, height: 1180 } },
  // HOSTING — close-up server Dell
  { src: 'e28c3f82-b26f-4750-849a-5b41335e6011', out: 'server-detaliu.webp',
    resize: 1600 },
  // DESPRE LAB — rack + monitor HA
  { src: 'd446f4be-6495-40ee-b9c0-33fc08858020', out: 'rack-infra.webp' },
  // IoT — banc prototipuri
  { src: '7469d1fa-48b8-4c73-a853-f0412230ebb4', out: 'prototipuri-iot.webp',
    resize: 1600 },
];

(async () => {
  for (const j of jobs) {
    let img = sharp(srcDir + j.src + '.jpg').rotate();
    if (j.extract) img = img.extract(j.extract);
    if (j.resize) img = img.resize({ width: j.resize, withoutEnlargement: true });
    img = grade(img);
    const info = await img.webp({ quality: 82, effort: 5 }).toFile(outDir + j.out);
    console.log(j.out.padEnd(22), info.width + 'x' + info.height, Math.round(info.size / 1024) + 'KB');
  }
  console.log('GATA');
})();
