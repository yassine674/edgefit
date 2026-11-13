// edgefit : une appli d'hébergement d'IA locale, à la manière d'Open WebUI.
// Barre latérale (carte, conversations, navigation) et pages choisies par l'adresse :
//   #/decouvrir   catalogue des modèles avec le verdict de chacun pour CETTE carte
//   #/modele?nom= verdict détaillé, optimisations, téléchargement
//   #/chat?id=    conversation (enregistrée sur la carte), mesures en direct à droite
//   #/modeles     modèles installés : chargé ou non, mémoire réelle, décharger, supprimer
//   #/bancs       bancs d'essai et score edgefit
//   #/materiel    mesures de la carte en direct
//   #/reglages    langue, contexte par défaut, cache KV déclaré du serveur
// Tout texte reçu du serveur est inséré avec textContent : jamais interprété comme du HTML.
"use strict";

const CONTEXTES = [2048, 4096, 8192, 16384, 32768, 65536, 131072];
const GIO = 1024 ** 3;
const HISTORIQUE = 60;

const etat = {
  langue: pref("edgefit.langue", "fr"),
  typeKv: pref("edgefit.kv", "f16"),
  numCtx: Number(pref("edgefit.ctx", "4096")) || 4096,
  ollama: { disponible: null },
  installes: [],
  charges: [],
  conversations: [],
  catalogue: null,
  bancs: [],
  metrique: null,
  historique: {},
  filtres: { verdict: new Set(), taille: null, tags: new Set(), installes: false, texte: "", tri: "score" },
  chat: null,
  telechargements: {},
  diagnostic: null,
  journal: [],
};

// --- outils --------------------------------------------------------------------------

function pref(cle, defaut) { try { return localStorage.getItem(cle) || defaut; } catch { return defaut; } }
function garder(cle, v) { try { localStorage.setItem(cle, v); } catch { /* sans importance */ } }
const T = () => TEXTES[etat.langue];
function t(cle, ...a) { const v = T()[cle]; return typeof v === "function" ? v(...a) : v; }
const f = (v, d = 1) => nombre(etat.langue, v, d);
const go = (v, d = 1) => `${f(v, d)} ${UNITE[etat.langue]}`;
const $ = (id) => document.getElementById(id);
const ctxCourt = (n) => (n >= 1024 ? `${n / 1024}K` : String(n));

function el(balise, classe, texte) {
  const e = document.createElement(balise);
  if (classe) e.className = classe;
  if (texte !== undefined && texte !== null) e.textContent = texte;
  return e;
}
function bouton(texte, classe, action) {
  const b = el("button", `bouton ${classe || ""}`.trim(), texte);
  b.type = "button";
  if (action) b.addEventListener("click", action);
  return b;
}
function icone(nom) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${nom}`);
  svg.append(use);
  svg.setAttribute("aria-hidden", "true");
  return svg;
}
function etiq(texte, accent) { return el("span", `etiq${accent ? " accent" : ""}`, texte); }
function pastille(verdict) {
  const [classe, cle] = verdict === "non" ? ["non", "v_non"] : verdict === "juste" ? ["juste", "v_juste"] : ["ok", "v_ok"];
  return el("span", `pastille ${classe}`, t(cle));
}

async function lireJson(r) { try { return await r.json(); } catch { return {}; } }
async function messageErreur(r) {
  const corps = await lireJson(r);
  const d = corps.detail;
  if (d && typeof d === "object" && !Array.isArray(d)) return T().erreurs[d.code] || (d.code === "carte_occupee" || d.code === "banc_en_cours" || d.code === "occupe" ? t("occupe") : d.message);
  if (r.status === 422) return T().erreurs.validation;
  if (r.status === 503) return T().erreurs.ollama;
  return corps.erreur || String(r.status);
}
function poster(url, corps, signal) {
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(corps), signal });
}
async function json(url, options) {
  const r = await fetch(url, options);
  if (!r.ok) throw new Error(await messageErreur(r));
  return r.json();
}
async function* ndjson(r) {
  const lecteur = r.body.getReader();
  const dec = new TextDecoder();
  let tampon = "";
  for (;;) {
    const { value, done } = await lecteur.read();
    if (done) break;
    tampon += dec.decode(value, { stream: true });
    let i;
    while ((i = tampon.indexOf("\n")) >= 0) {
      const ligne = tampon.slice(0, i);
      tampon = tampon.slice(i + 1);
      if (ligne.trim()) yield JSON.parse(ligne);
    }
  }
  if (tampon.trim()) yield JSON.parse(tampon);
}

function blocCommande(texte) {
  const bloc = el("div", "bloc-cmd");
  const pre = el("pre", "code");
  pre.append(el("code", null, texte));
  const copier = bouton(t("commande_copier"), "petit copier", async () => {
    try {
      await navigator.clipboard.writeText(texte);
      copier.textContent = t("commande_copie");
      setTimeout(() => { copier.textContent = t("commande_copier"); }, 1500);
    } catch {
      const plage = document.createRange();
      plage.selectNodeContents(pre);
      getSelection().removeAllRanges();
      getSelection().addRange(plage);
    }
  });
  bloc.append(pre, copier);
  return bloc;
}
function pliCommande(contenu) {
  const d = el("details", "cmd");
  d.append(el("summary", null, t("commande_afficher")), ...contenu);
  return d;
}
function commandeKv(kv) {
  return { ouvrir: "sudo systemctl edit ollama",
    contenu: `[Service]\nEnvironment="OLLAMA_FLASH_ATTENTION=1"\nEnvironment="OLLAMA_KV_CACHE_TYPE=${kv}"`,
    apres: "sudo systemctl restart ollama" };
}
function pliKv(kv, c = commandeKv(kv)) {
  return pliCommande([el("p", "aide", t("kv_ouvrir")), blocCommande(c.ouvrir), el("p", "aide", t("kv_coller")),
    blocCommande(c.contenu), el("p", "aide", t("kv_redemarrer")), blocCommande(c.apres)]);
}

function selectContexte(valeur, max, surChange) {
  const s = el("select");
  const valeurs = CONTEXTES.filter((c) => !max || c <= max);
  if (!valeurs.includes(valeur)) valeur = valeurs.filter((v) => v <= valeur).at(-1) || valeurs[0];
  s.append(...valeurs.map((c) => new Option(t("contexte_option", c), c)));
  s.value = valeur;
  s.addEventListener("change", () => surChange(Number(s.value)));
  return s;
}
function selectKv(valeur, surChange) {
  const s = el("select");
  s.append(new Option(t("kv_defaut"), "f16"), new Option("q8_0", "q8_0"), new Option("q4_0", "q4_0"));
  s.value = valeur;
  s.title = t("kv_aide");
  s.addEventListener("change", () => surChange(s.value));
  return s;
}
function champ(libelle, controle) {
  const l = el("label", "champ");
  l.append(el("span", "libelle", libelle), controle);
  return l;
}

// anneau de score : plein (dégradé) si mesuré, rien sinon
function anneau(valeur, grand = false) {
  const a = el("div", `anneau-score${grand ? " grand" : ""}`);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 40 40");
  const fond = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  const val = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  for (const c of [fond, val]) { c.setAttribute("cx", "20"); c.setAttribute("cy", "20"); c.setAttribute("r", "17"); }
  fond.setAttribute("class", "fond");
  val.setAttribute("class", "val");
  const tour = 2 * Math.PI * 17;
  val.setAttribute("stroke-dasharray", `${(valeur / 100) * tour} ${tour}`);
  svg.append(fond, val);
  a.append(svg, el("span", null, String(valeur)));
  a.title = `${t("rail_score")} ${valeur}/100`;
  return a;
}

function courbe(points, max) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "courbe");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  if (!points.length) return svg;
  const echelle = max || Math.max(...points, 1);
  const pas = 100 / Math.max(points.length - 1, 1);
  const p = points.map((v, i) => `${(i * pas).toFixed(2)},${(100 - (v / echelle) * 90 - 5).toFixed(2)}`);
  if (points.length === 1) p.push(`100,${p[0].split(",")[1]}`);
  const aire = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
  aire.setAttribute("points", `0,100 ${p.join(" ")} 100,100`);
  const ligne = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  ligne.setAttribute("points", p.join(" "));
  svg.append(aire, ligne);
  return svg;
}

function confirmer(titre, texte, oui) {
  return new Promise((resolve) => {
    const d = $("dialogue");
    $("dialogue-titre").textContent = titre;
    $("dialogue-texte").textContent = texte;
    $("dialogue-oui").textContent = oui;
    $("dialogue-non").textContent = t("annuler");
    const fin = (r) => { d.close(); resolve(r); };
    $("dialogue-oui").onclick = () => fin(true);
    $("dialogue-non").onclick = () => fin(false);
    d.onclose = () => resolve(false);
    d.showModal();
  });
}

// --- données partagées ---------------------------------------------------------------

async function chargerOllama() {
  try { etat.ollama = await json("/api/ollama"); } catch { etat.ollama = { disponible: false }; }
  try { etat.installes = etat.ollama.disponible ? await json("/api/modeles") : []; } catch { etat.installes = []; }
  try { etat.charges = etat.ollama.disponible ? await json("/api/charges") : []; } catch { etat.charges = []; }
  const o = etat.ollama;
  $("point-ollama").className = `point ${o.disponible ? "ok" : "bad"}`;
  $("texte-ollama").textContent = !o.disponible ? t("ollama_absent") : o.version === "simulé" ? t("ollama_simule") : t("ollama_ok", o.version);
  $("compte-modeles").textContent = etat.installes.length || "";
  $("banniere").hidden = o.disponible !== false;
  $("banniere").textContent = t("ollama_banniere", o.url || "127.0.0.1:11434");
  if (!o.disponible) setTimeout(async () => { await chargerOllama(); if (etat.ollama.disponible) afficher(); }, 10000);
}
async function chargerConversations() {
  try { etat.conversations = await json("/api/conversations"); } catch { etat.conversations = []; }
  rendreConversations();
}
async function chargerBancs() {
  try { etat.bancs = await json("/api/bancs"); } catch { etat.bancs = []; }
}
const dernierBanc = (nom) => etat.bancs.find((b) => b.modele === nom && b.score);

// --- barre latérale ----------------------------------------------------------------------

function rendreConversations() {
  const zone = $("liste-conv");
  zone.replaceChildren();
  if (!etat.conversations.length) { zone.append(el("p", "vide-conv", t("conv_aucune"))); return; }
  const jour = (ts) => { const d = new Date(ts * 1000); d.setHours(0, 0, 0, 0); return d.getTime(); };
  const auj = new Date(); auj.setHours(0, 0, 0, 0);
  const groupes = [["conv_aujourdhui", []], ["conv_hier", []], ["conv_avant", []]];
  for (const c of etat.conversations) {
    const ecart = (auj.getTime() - jour(c.modifie)) / 86400000;
    groupes[ecart <= 0 ? 0 : ecart <= 1 ? 1 : 2][1].push(c);
  }
  const actif = etat.chat?.conv?.id;
  for (const [cle, convs] of groupes) {
    if (!convs.length) continue;
    zone.append(el("p", "libelle", t(cle)));
    for (const c of convs) {
      const ligne = el("div", `conv${c.id === actif ? " active" : ""}`);
      const a = el("a", null, c.titre || t("conv_sans_titre"));
      a.href = `#/chat?id=${c.id}`;
      a.title = `${c.titre || t("conv_sans_titre")} · ${c.modele}`;
      const s = el("button", "suppr", "×");
      s.type = "button";
      s.setAttribute("aria-label", t("conv_supprimer"));
      s.addEventListener("click", async () => {
        await fetch(`/api/conversations/${c.id}`, { method: "DELETE" });
        if (etat.chat?.conv?.id === c.id) location.hash = "#/chat";
        chargerConversations();
      });
      ligne.append(a, s);
      zone.append(ligne);
    }
  }
}
$("nouvelle-conv").addEventListener("click", () => {
  if (location.hash.startsWith("#/chat") && !etat.chat?.conv) return;
  location.hash = "#/chat";
});
$("ouvrir-barre").addEventListener("click", () => {
  const ouverte = $("barre").classList.toggle("ouverte");
  $("ouvrir-barre").setAttribute("aria-expanded", String(ouverte));
});
$("barre").addEventListener("click", (e) => { if (e.target.closest("a")) $("barre").classList.remove("ouverte"); });

// barre de RAM : système, modèles chargés, libre ; et le « fantôme » du modèle survolé
function majRam() {
  const m = etat.metrique;
  if (!m) return;
  const total = m.ram_totale_go;
  const charge = etat.charges.reduce((s, c) => s + c.taille_go, 0);
  const sys = Math.max(0, m.ram_utilisee_go - charge);
  $("ram-sys").style.width = `${(sys / total) * 100}%`;
  $("ram-mod").style.width = `${(Math.min(charge, total - sys) / total) * 100}%`;
  $("ram-texte").replaceChildren(el("strong", null, f(m.ram_utilisee_go, 1)), ` / ${go(total, 1)}`);
}
function montrerFantome(modele) {
  const m = etat.metrique;
  const g = $("fantome");
  if (!m || !modele) { g.hidden = true; $("ram-fantome").textContent = ""; return; }
  const total = m.ram_totale_go;
  const gauche = Math.min(m.ram_utilisee_go / total, 1);
  g.hidden = false;
  g.style.left = `${gauche * 100}%`;
  g.style.width = `${Math.min(modele.total_go / total, 1 - gauche) * 100}%`;
  const ok = modele.verdict !== "trop_gros";
  g.classList.toggle("deborde", !ok);
  $("ram-fantome").textContent = t("fantome", go(modele.total_go, 1), ok);
}

// --- métriques en direct ---------------------------------------------------------------

const MESURES = [
  { cle: "cpu_pct", titre: "m_cpu", unite: "%", max: 100, d: 0 },
  { cle: "gpu_pct", titre: "m_gpu", unite: "%", max: 100, d: 0 },
  { cle: "ram_utilisee_go", titre: "m_ram", unite: "go", maxCle: "ram_totale_go", d: 1 },
  { cle: "puissance_w", titre: "m_puissance", unite: "W", d: 1 },
  { cle: "temp_cpu_c", titre: "m_temp_cpu", unite: "°C", max: 100, d: 0 },
  { cle: "temp_gpu_c", titre: "m_temp_gpu", unite: "°C", max: 100, d: 0 },
];
function recevoirMetrique(m) {
  etat.metrique = m;
  $("nom-carte").textContent = m.machine;
  for (const mes of MESURES) {
    const h = (etat.historique[mes.cle] ||= []);
    if (m[mes.cle] !== null && m[mes.cle] !== undefined) h.push(m[mes.cle]);
    if (h.length > HISTORIQUE) h.shift();
  }
  for (const a of m.alertes) {
    const derniere = etat.journal.at(-1);
    if (!derniere || derniere.texte !== a) {
      etat.journal.push({ texte: a, heure: new Date().toLocaleTimeString(etat.langue === "fr" ? "fr-FR" : "en-US") });
      if (etat.journal.length > 50) etat.journal.shift();
    }
  }
  majRam();
  if (etat.page === "materiel") rendreMateriel();
  if (etat.page === "chat") majRailMateriel();
}
function connecter(delai = 1000) {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/metriques`);
  ws.onopen = () => { delai = 1000; };
  ws.onmessage = (e) => recevoirMetrique(JSON.parse(e.data));
  ws.onclose = async () => {
    try { if ((await fetch("/api/metriques")).status === 401) { $("nom-carte").textContent = t("acces_expire"); return; } } catch { /* on réessaie */ }
    setTimeout(() => connecter(Math.min(delai * 2, 10000)), delai);
  };
}

// --- navigation ----------------------------------------------------------------------------

function route() {
  const [chemin, requete] = location.hash.replace(/^#\/?/, "").split("?");
  return { page: chemin || "", params: Object.fromEntries(new URLSearchParams(requete || "")) };
}
function afficher() {
  let { page, params } = route();
  const pages = ["decouvrir", "modele", "chat", "modeles", "bancs", "materiel", "reglages"];
  if (!pages.includes(page)) {
    location.replace(etat.installes.length ? "#/chat" : "#/decouvrir");
    return;
  }
  etat.page = page;
  for (const a of document.querySelectorAll(".nav a")) {
    if (a.dataset.page === page) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  montrerFantome(null);
  const titres = { decouvrir: "nav_decouvrir", modeles: "nav_modeles", bancs: "nav_bancs", materiel: "nav_materiel", reglages: "nav_reglages", chat: "nouvelle_conv", modele: "nav_decouvrir" };
  $("titre-mobile").textContent = t(titres[page]);
  const zone = $("page");
  zone.className = "";
  scrollTo(0, 0);
  ({ decouvrir: pageDecouvrir, modele: () => pageModele(params.nom), chat: () => pageChat(params.id),
    modeles: pageModeles, bancs: pageBancs, materiel: pageMateriel, reglages: pageReglages })[page]();
  rendreConversations();
}
addEventListener("hashchange", afficher);

function titrePage(titre, sous, droite) {
  const h = el("div", "page-titre");
  const g = el("div");
  g.append(el("h1", null, titre));
  if (sous) g.append(el("p", null, sous));
  h.append(g);
  if (droite) h.append(droite);
  return h;
}

// --- page Découvrir ----------------------------------------------------------------------

function verdictCarte(m, utilisable) {
  if (m.verdict === "trop_gros") return "non";
  return m.reste_go < 0.15 * utilisable ? "juste" : "ok";
}
function classeTaille(m) {
  const n = parseFloat(m.parametres) * (/M$/i.test(m.parametres) ? 1e-3 : 1);
  return n < 1 ? "petit" : n <= 3.2 ? "moyen" : "grand";
}

async function pageDecouvrir() {
  const zone = $("page");
  zone.className = "page";
  const reglages = el("div", "groupe-champs");
  const tri = el("select");
  for (const [k, lib] of Object.entries(T().tri)) tri.append(new Option(lib, k));
  tri.value = etat.filtres.tri;
  tri.addEventListener("change", () => { etat.filtres.tri = tri.value; rendreGrille(); });
  reglages.append(
    champ(t("trier"), tri),
    champ(t("contexte"), selectContexte(etat.numCtx, null, (v) => { etat.numCtx = v; garder("edgefit.ctx", String(v)); chargerCatalogue(); })),
    champ(t("kv_serveur"), selectKv(etat.typeKv, (v) => { etat.typeKv = v; garder("edgefit.kv", v); chargerCatalogue(); })));
  const sous = el("p");
  sous.id = "dec-sous";
  const entete = titrePage(t("dec_titre"), "", reglages);
  entete.querySelector("div").append(sous);
  const recherche = el("input", "recherche-catalogue");
  recherche.type = "search";
  recherche.placeholder = t("dec_chercher");
  recherche.value = etat.filtres.texte;
  recherche.setAttribute("aria-label", t("dec_chercher"));
  let minuterie;
  recherche.addEventListener("input", () => {
    etat.filtres.texte = recherche.value.trim();
    rendreGrille();
    clearTimeout(minuterie);
    minuterie = setTimeout(chercherHf, 300);
  });
  const filtres = el("div", "filtres");
  filtres.id = "filtres";
  const contenu = el("div");
  contenu.id = "catalogue";
  const hf = el("div");
  hf.id = "zone-hf";
  const bandeau = el("div", "bandeau-carte");
  bandeau.id = "bandeau-carte";
  zone.replaceChildren(bandeau, entete, recherche, filtres, contenu, hf);
  contenu.append(el("div", "squelette"));
  await chargerCatalogue();
  if (etat.filtres.texte) chercherHf();
}

async function chargerCatalogue() {
  try {
    etat.catalogue = await json(`/api/catalogue?num_ctx=${etat.numCtx}&type_kv=${etat.typeKv}`);
  } catch (err) {
    $("catalogue")?.replaceChildren(el("p", "erreur-bloc", err.message));
    return;
  }
  await chargerBancs();
  const c = etat.catalogue;
  $("dec-sous") && ($("dec-sous").textContent = t("dec_sous", go(c.budget_go.utilisable, 1), ctxCourt(c.num_ctx), c.type_kv));
  const bandeau = $("bandeau-carte");
  if (bandeau) {
    const morceaux = t("bandeau", go(c.budget_go.utilisable, 1), c.charges.length, etat.installes.length, derniersParModele().length);
    bandeau.replaceChildren(el("span", "nom-carte", etat.metrique?.machine || ""), ...morceaux.map((x) => el("span", "aide", x)));
  }
  rendreFiltres();
  rendreGrille();
}

function rendreFiltres() {
  const zone = $("filtres");
  if (!zone || !etat.catalogue) return;
  const F = etat.filtres;
  const u = etat.catalogue.budget_go.utilisable;
  const modeles = etat.catalogue.modeles;
  const puce = (texte, actif, action, n, point) => {
    const p = el("button", "puce");
    p.type = "button";
    p.setAttribute("aria-pressed", String(actif));
    if (point) p.append(el("span", `point ${point}`));
    p.append(texte);
    if (n !== undefined) p.append(el("span", "n", String(n)));
    p.addEventListener("click", () => { action(); rendreFiltres(); rendreGrille(); });
    return p;
  };
  const basculer = (ens, v) => (ens.has(v) ? ens.delete(v) : ens.add(v));
  const nb = (pred) => modeles.filter(pred).length;
  zone.replaceChildren(
    puce(t("f_tient"), F.verdict.has("ok"), () => basculer(F.verdict, "ok"), nb((m) => verdictCarte(m, u) === "ok"), "ok"),
    puce(t("f_juste"), F.verdict.has("juste"), () => basculer(F.verdict, "juste"), nb((m) => verdictCarte(m, u) === "juste"), "juste"),
    puce(t("f_non"), F.verdict.has("non"), () => basculer(F.verdict, "non"), nb((m) => verdictCarte(m, u) === "non"), "bad"),
    el("span", "sep"),
    ...Object.entries(T().f_tailles).map(([k, lib]) => puce(lib, F.taille === k, () => { F.taille = F.taille === k ? null : k; })),
    el("span", "sep"),
    ...Object.entries(T().tags).map(([k, lib]) => puce(lib, F.tags.has(k), () => basculer(F.tags, k))),
    puce(t("f_installes"), F.installes, () => { F.installes = !F.installes; }, nb((m) => m.installe)),
  );
}

function rendreGrille() {
  const zone = $("catalogue");
  if (!zone || !etat.catalogue) return;
  const F = etat.filtres;
  const u = etat.catalogue.budget_go.utilisable;
  const texte = F.texte.toLowerCase();
  let modeles = etat.catalogue.modeles.filter((m) =>
    (!F.verdict.size || F.verdict.has(verdictCarte(m, u))) && (!F.taille || classeTaille(m) === F.taille)
    && [...F.tags].every((tg) => m.tags.includes(tg)) && (!F.installes || m.installe)
    && (!texte || m.nom.includes(texte) || m.description.toLowerCase().includes(texte) || (m.famille || "").includes(texte)));
  const cle = { score: (m) => [m.verdict === "trop_gros" ? 1 : 0, -(m.score?.score ?? -1), -m.taille_go],
    marge: (m) => [-m.reste_go], taille: (m) => [m.taille_go] }[F.tri];
  modeles = modeles.slice().sort((a, b) => { const x = cle(a), y = cle(b); for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] - y[i]; return 0; });

  const contenu = [];
  const filtre = F.verdict.size || F.taille || F.tags.size || F.installes || texte;
  if (!filtre) {
    const recos = recommandes(etat.catalogue.modeles, u);
    modeles = modeles.filter((m) => !recos.includes(m));
    if (recos.length) {
      const st = el("div", "section-titre");
      st.append(el("span", "libelle", t("recommandes")), el("span", "aide", t("reco_raison")));
      const g = el("div", "grille");
      g.append(...recos.map((m) => carteModele(m, u, true)));
      contenu.push(st, g);
    }
  }
  const st = el("div", "section-titre");
  st.append(el("span", "libelle", t("tous_modeles", modeles.length)));
  const g = el("div", "grille");
  g.append(...modeles.map((m) => carteModele(m, u)));
  contenu.push(st, modeles.length ? g : el("p", "aide", t("aucun_filtre")));
  zone.replaceChildren(...contenu);
}

function recommandes(modeles, u) {
  const confort = modeles.filter((m) => m.verdict !== "trop_gros" && m.reste_go >= 0.2 * u).sort((a, b) => b.taille_go - a.taille_go);
  const choix = [];
  const prendre = (pred) => { const m = confort.find((x) => !choix.includes(x) && pred(x)); if (m) choix.push(m); };
  prendre(() => true);
  prendre((m) => m.tags.includes("raisonnement"));
  prendre((m) => m.tags.includes("code") || m.tags.includes("vision"));
  prendre(() => true);
  return choix.slice(0, 3);
}

function carteModele(m, u, reco = false) {
  const v = verdictCarte(m, u);
  const carte = el("article", `carte-modele${reco ? " recommande" : ""}`);
  carte.addEventListener("mouseenter", () => montrerFantome(m));
  carte.addEventListener("mouseleave", () => montrerFantome(null));
  carte.addEventListener("focusin", () => montrerFantome(m));
  carte.addEventListener("focusout", () => montrerFantome(null));
  const tete = el("div", "carte-tete");
  tete.append(el("h3", null, m.nom), pastille(v));
  const etiqs = el("div", "etiqs");
  etiqs.append(etiq(m.famille || "?"), etiq(m.parametres), etiq(m.format), ...m.tags.map((tg) => etiq(T().tags[tg] || tg)));
  if (m.installe) etiqs.append(etiq(t("badge_installe"), true));
  const mem = el("div", "mini-memoire");
  const barre = el("div", `mini-barre ${v}`);
  const rempli = el("span");
  rempli.style.width = `${Math.min(100, (m.total_go / u) * 100)}%`;
  barre.append(rempli);
  const ligne = el("div", "ligne");
  ligne.append(el("span", null, `${go(m.total_go, 2)} / ${go(u, 1)}`),
    el("span", m.reste_go >= 0 ? "reste" : "manque", m.reste_go >= 0 ? t("reste", go(m.reste_go, 2)) : t("manque", go(-m.reste_go, 2))));
  mem.append(barre, ligne);
  const bas = el("div", "carte-bas");
  const vit = el("div", "vitesse-estimee", m.plafond_tokens_s ? t("plafond", f(m.plafond_tokens_s, 0)) : "");
  if (m.plafond_tokens_s) vit.append(el("small", null, t("plafond_sous")));
  const banc = dernierBanc(m.nom);
  if (banc) {
    const z = el("div", "score-ligne");
    z.append(el("span", "aide", t("mesure_badge", f(banc.mesures.debit_tokens_s, 0))), anneau(banc.score.score));
    bas.append(vit, z);
  } else bas.append(vit, el("span", "aide", t("jamais_mesure")));
  const actions = el("div", "carte-actions");
  actions.id = `actions-${m.nom}`;
  if (m.installe && v !== "non") actions.append(bouton(t("a_discuter_court"), "principal", () => nouvelleConversationAvec(m.nom)));
  else if (v !== "non") {
    const b = bouton(t("a_telecharger", go(m.taille_go, 1)), "principal", () => telechargerCarte(m, actions));
    b.disabled = !etat.ollama.disponible;
    actions.append(b);
  } else actions.append(Object.assign(bouton(t("a_ne_tient_pas")), { disabled: true }));
  const det = el("a", "bouton", t("a_details"));
  det.href = `#/modele?nom=${encodeURIComponent(m.nom)}&ctx=${etat.numCtx}&kv=${etat.typeKv}`;
  actions.append(det);
  carte.append(tete, el("p", "desc", m.description), etiqs, mem, bas, actions);
  return carte;
}

async function telecharger(nom, surProgres) {
  const r = await poster("/api/modeles/telecharger", { nom });
  if (!r.ok) throw new Error(await messageErreur(r));
  let depart = null;
  for await (const evt of ndjson(r)) {
    if (evt.type === "erreur") throw new Error(evt.erreur);
    if (evt.type === "fin") return;
    const etiquette = Object.entries(t("dl_etat")).find(([cle]) => (evt.statut || "").startsWith(cle));
    let texte = etiquette ? etiquette[1] : evt.statut, part = null;
    if (evt.total && evt.total > 50e6) {
      depart ??= { t: performance.now(), fait: evt.fait || 0 };
      const ecoule = (performance.now() - depart.t) / 1000;
      const vitesse = ecoule > 1 ? ((evt.fait || 0) - depart.fait) / ecoule : 0;
      part = (evt.fait || 0) / evt.total;
      texte = `${texte} · ${t("dl_progression", go((evt.fait || 0) / GIO, 2), go(evt.total / GIO, 2),
        vitesse ? `${f(vitesse / 1e6, 1)} M${etat.langue === "fr" ? "o" : "B"}` : null, null)}`;
    }
    surProgres(texte, part);
  }
}
function avertirQuitter(e) { e.preventDefault(); e.returnValue = t("dl_quitter"); }

async function telechargerCarte(m, actions) {
  const zone = el("div", "progres-carte");
  const texte = el("span", null, t("dl_etat")["pulling manifest"]);
  const barre = el("div", "barre-progres");
  const rempli = el("span");
  barre.append(rempli);
  zone.append(texte, barre);
  actions.replaceChildren(zone);
  addEventListener("beforeunload", avertirQuitter);
  try {
    await telecharger(m.nom, (txt, part) => { texte.textContent = txt; if (part !== null) rempli.style.width = `${part * 100}%`; });
    removeEventListener("beforeunload", avertirQuitter);
    await chargerOllama();
    nouvelleConversationAvec(m.nom);
  } catch (err) {
    removeEventListener("beforeunload", avertirQuitter);
    actions.replaceChildren(el("p", "erreur-bloc", err.message));
  }
}

async function chercherHf() {
  const zone = $("zone-hf");
  if (!zone) return;
  const q = etat.filtres.texte;
  if (q.length < 2) { zone.replaceChildren(); return; }
  let d;
  try { d = await json(`/api/recherche?q=${encodeURIComponent(q)}`); } catch { return; }
  if (etat.filtres.texte !== q) return;
  const st = el("div", "section-titre");
  st.append(el("span", "libelle", t("hf_titre")), el("span", "aide", t("hf_aide")));
  if (d.erreur_hf) { zone.replaceChildren(st, el("p", "aide", t(`hf_${d.erreur_hf}`) || t("hf_indisponible"))); return; }
  if (!d.huggingface.length) { zone.replaceChildren(); return; }
  const liste = el("div", "hf-liste");
  for (const h of d.huggingface) {
    const ligne = bouton("", "hf-ligne", () => montrerVersions(h.depot, ligne));
    ligne.className = "hf-ligne";
    const droite = el("span", "aide", t("telechargements", f(h.telechargements, 0)));
    if (h.protege) droite.append(" · ", t("badge_licence"));
    ligne.append(el("span", "mono", h.depot), droite);
    liste.append(ligne);
  }
  zone.replaceChildren(st, liste);
}

async function montrerVersions(depot, apres) {
  document.querySelector(".versions-hf")?.remove();
  const zone = el("div", "versions-hf");
  zone.append(el("div", "squelette"));
  apres.after(zone);
  try {
    const versions = await json(`/api/variantes?depot=${encodeURIComponent(depot)}`);
    const base = versions.find((v) => v.format === "Q4_K_M");
    const liste = el("div", "hf-liste");
    for (const v of versions) {
      const b = bouton("", "version", () => { location.hash = `#/modele?nom=${encodeURIComponent(v.nom)}&ctx=${etat.numCtx}&kv=${etat.typeKv}`; });
      b.className = "version";
      b.disabled = !v.telechargeable;
      const bits = base ? (4.89 * v.taille_octets) / base.taille_octets : null;
      b.append(el("span", "mono", v.format), el("span", "q", !v.telechargeable ? t("version_fragmentee") : bits ? t("qualite", bits) : ""),
        el("span", "mono", go((v.taille_octets + v.projecteur_octets) / GIO, 2)), el("span", "aide", "→"));
      liste.append(b);
    }
    zone.replaceChildren(liste);
  } catch (err) {
    zone.replaceChildren(el("p", "erreur-bloc", err.message));
  }
}

async function nouvelleConversationAvec(nom) {
  try {
    const conv = await json("/api/conversations", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ modele: nom, num_ctx: etat.numCtx }) });
    await chargerConversations();
    location.hash = `#/chat?id=${conv.id}`;
  } catch (err) { alert(err.message); }
}

// --- page Modèle (verdict détaillé) ----------------------------------------------------

async function pageModele(nom) {
  const zone = $("page");
  zone.className = "page";
  const p = route().params;
  if (p.ctx) etat.numCtx = Number(p.ctx) || etat.numCtx;
  if (["f16", "q8_0", "q4_0"].includes(p.kv)) etat.typeKv = p.kv;
  const retour = bouton(t("m_retour"), "", () => { location.hash = "#/decouvrir"; });
  retour.className = "retour";
  zone.replaceChildren(retour, titrePage(nom), el("div", "squelette"));
  let d;
  try {
    d = await json("/api/compatibilite", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ modele: nom, num_ctx: etat.numCtx, type_kv: etat.typeKv }) });
  } catch (err) {
    zone.replaceChildren(retour, titrePage(nom), el("p", "erreur-bloc", err.message));
    return;
  }
  if (route().params.nom !== nom) return;
  const tete = titrePage(d.modele);
  const etiqs = el("div", "etiqs");
  etiqs.style.marginTop = "8px";
  etiqs.append(etiq(d.source === "installé" ? t("source_installe") : t("source_registre"), d.source === "installé"));
  if (d.format) etiqs.append(etiq(d.format));
  if (d.projecteur_go) etiqs.append(etiq(t("badge_vision")));
  etiqs.append(etiq(go(d.taille_fichier_go + d.projecteur_go, 2)));
  if (d.contexte_max) etiqs.append(etiq(`max ${ctxCourt(d.contexte_max)}`));
  tete.querySelector("div").append(etiqs);

  const grille = el("div", "modele-grille");
  const gauche = el("div", "panneau");
  gauche.append(...blocVerdict(d));
  const reco = d.verdict === "trop_gros" ? solutionQuiSuffit(d) : null;
  if (reco) gauche.append(reco);
  gauche.append(courbeContexte(d), zoneActions(d));
  const droite = el("div", "colonne-droite");
  droite.append(boitePieces(d), blocArchitecture(d));
  const banc = dernierBanc(d.modele);
  if (banc) {
    const p = el("section", "panneau");
    const z = el("div", "score-ligne");
    z.append(anneau(banc.score.score), el("span", "aide", t("b_phrase", banc.score, banc.mesures, f)));
    const dl = el("dl", "lignes-mesure");
    dl.style.marginTop = "10px";
    if (banc.mesures.memoire_go) dl.append(el("dt", null, t("rail_mesuree")), el("dd", null, go(banc.mesures.memoire_go, 2)), el("dt", null, t("rail_estimee")), el("dd", null, go(d.estimation_go.total, 2)));
    p.append(el("span", "libelle", t("m_mesure")), z, dl);
    droite.append(p);
  }
  grille.append(gauche, droite);
  const contenu = [retour, tete, grille];
  if (d.verdict === "trop_gros") contenu.push(listeOptimisations(d));
  else if (d.piles.some((x) => x.cle !== "tel_quel")) contenu.push(blocCombinaisons(d, t("autres_reglages")));
  zone.replaceChildren(...contenu);
}

function reverifier() {
  location.hash = `#/modele?nom=${encodeURIComponent(route().params.nom)}&ctx=${etat.numCtx}&kv=${etat.typeKv}`;
}

function blocVerdict(d) {
  const e = d.estimation_go, b = d.budget_go;
  const v = d.verdict === "trop_gros" ? "non" : d.verdict === "ok_apres_dechargement" || b.utilisable - e.total < 0.15 * b.utilisable ? "juste" : "ok";
  const decharges = (d.charges || []).filter((c) => c.nom !== d.modele).map((c) => c.nom);
  const titre = d.verdict === "ok" ? t("verdict_ok") : d.verdict === "ok_apres_dechargement" ? t("verdict_decharge", decharges.join(", ") || "…") : t("verdict_trop", go(d.solutions.manque_go, 2));
  const sous = d.verdict === "ok" ? t("statut_ok_sous", go(b.utilisable - e.total, 2)) : d.verdict === "ok_apres_dechargement" ? t("statut_decharge_sous") : t("statut_trop_sous");
  const statut = el("div", `statut ${v}`);
  const ic = el("div", "statut-icone");
  ic.append(icone(v === "non" ? "non" : v === "juste" ? "juste" : "ok"));
  const tx = el("div");
  tx.append(el("h2", null, titre), el("p", null, sous));
  statut.append(ic, tx);

  const jauge = el("div", "jauge");
  const haut = el("div", "jauge-haut");
  haut.append(el("strong", null, go(e.total, 2)), el("span", null, `${t("necessaire")} · ${t("sur_utilisables", go(b.utilisable, 2))}`));
  const echelle = Math.max(e.total, b.utilisable) * 1.04 || 1;
  const barre = el("div", "jauge-barre");
  for (const [cle, val] of [["poids", e.poids], ["kv", e.cache_kv], ["buffers", e.buffers]]) {
    const s = el("span", `seg ${cle}`);
    s.style.width = `${(val / echelle) * 100}%`;
    barre.append(s);
  }
  if (e.total > b.utilisable) {
    const h = el("span", "hachure");
    h.style.left = `${(b.utilisable / echelle) * 100}%`;
    h.style.width = `${((e.total - b.utilisable) / echelle) * 100}%`;
    barre.append(h);
  }
  const lim = el("span", "jauge-limite");
  lim.style.left = `${(b.utilisable / echelle) * 100}%`;
  barre.append(lim);
  const leg = el("div", "legende");
  for (const [lib, c] of [["p_poids", "var(--a)"], ["p_kv", "var(--b)"], ["p_buffers", "#5a6070"]]) {
    const s = el("span"); const i = el("i"); i.style.background = c; s.append(i, t(lib)); leg.append(s);
  }
  const s = el("span"); const i = el("i"); i.style.cssText = "width:2px;background:var(--t1)"; s.append(i, t("l_limite")); leg.append(s);
  jauge.append(haut, barre, leg);

  const reg = el("div", "groupe-champs");
  reg.style.marginTop = "18px";
  reg.append(champ(t("contexte"), selectContexte(d.num_ctx, d.contexte_max, (v2) => { etat.numCtx = v2; garder("edgefit.ctx", String(v2)); reverifier(); })),
    champ(t("kv_serveur"), selectKv(d.type_kv, (v2) => { etat.typeKv = v2; garder("edgefit.kv", v2); reverifier(); })));
  return [statut, jauge, reg, el("p", "aide", t("contexte_aide"))];
}

// la piste la plus simple qui suffit à elle seule, sinon une combinaison qui tient, sinon un modèle plus petit
function solutionQuiSuffit(d) {
  const ordre = ["contexte", "variante", "cache_kv", "parallele"];
  let piste = null;
  for (const cat of d.solutions.categories) {
    piste = cat.pistes.find((p) => p.rentre && ordre.includes(p.type) && (p.type !== "variante" || (p.nom && p.bits >= 4.5)));
    if (piste) break;
  }
  const pile = !piste && d.piles.find((x) => x.cle !== "tel_quel" && !x.qualite_degradee && (!x.telechargement || x.nom));
  if (!piste && !pile) piste = d.solutions.categories.find((c) => c.cle === "modele")?.pistes[0];
  if (!piste && !pile) return null;
  const z = el("div", "reco-ligne");
  const tx = el("div");
  let action;
  if (piste) {
    const [titre, detail] = textePiste(piste);
    tx.append(el("h3", "titre-reco", `${t("reco_titre")} : ${titre}`), el("p", "aide", detail));
    action = piste.type === "contexte"
      ? bouton(t("action_appliquer"), "principal", () => { etat.numCtx = piste.valeur; garder("edgefit.ctx", String(piste.valeur)); reverifier(); })
      : piste.nom ? bouton(t("action_verifier", piste.type === "variante" ? piste.format : piste.nom), "principal", () => { location.hash = `#/modele?nom=${encodeURIComponent(piste.nom)}&ctx=${etat.numCtx}&kv=${etat.typeKv}`; })
        : null;
    if (piste.type === "cache_kv") tx.append(pliKv(piste.valeur, piste.commande));
  } else {
    tx.append(el("h3", "titre-reco", `${t("reco_titre")} : ${T().pile[pile.cle]}`), el("p", "aide mono", t("pile_reglage", pile.format, pile.num_ctx, pile.type_kv)));
    action = bouton(t("pile_choisir"), "principal", () => {
      etat.numCtx = pile.num_ctx;
      if (pile.reglage_serveur) { etat.typeKv = pile.type_kv; garder("edgefit.kv", pile.type_kv); }
      location.hash = `#/modele?nom=${encodeURIComponent(pile.telechargement && pile.nom ? pile.nom : d.modele)}&ctx=${etat.numCtx}&kv=${etat.typeKv}`;
    });
    if (pile.reglage_serveur) tx.append(pliKv(pile.type_kv));
  }
  z.append(tx);
  if (action) z.append(action);
  return z;
}

// mémoire totale selon le contexte : seul le cache KV change (proportionnel au contexte)
function courbeContexte(d) {
  const a = d.architecture;
  const e = d.estimation_go, u = d.budget_go.utilisable;
  const ctxs = CONTEXTES.filter((c) => !a.context_max || c <= a.context_max);
  const totaux = ctxs.map((c) => (e.poids + e.cache_kv * (c / d.num_ctx)) * 1.1);
  const max = Math.max(...totaux, u) * 1.12;
  const bloc = el("div", "bloc-courbe");
  const graph = el("div", "courbe-ctx");
  for (const [i, c] of ctxs.entries()) {
    const col = el("div", `col${totaux[i] > u ? " deborde" : ""}${c === d.num_ctx ? " actuel" : ""}`);
    const barre = el("span");
    barre.style.height = `${(totaux[i] / max) * 100}%`;
    col.append(el("em", null, f(totaux[i], 1)), barre, el("small", null, ctxCourt(c)));
    graph.append(col);
  }
  const lim = el("span", "limite-ctx");
  lim.style.bottom = `${18 + (u / max) * 102}px`;
  graph.append(lim);
  bloc.append(el("span", "libelle", t("m_courbe")), graph);
  return bloc;
}

function blocArchitecture(d) {
  const a = d.architecture;
  const p = el("section", "panneau");
  p.append(el("span", "libelle", t("m_archi")));
  const dl = el("dl", "lignes-mesure");
  dl.style.marginTop = "10px";
  const octets = { f16: 2, q8_0: 34 / 32, q4_0: 18 / 32 }[d.type_kv] || 2;
  const parToken = 2 * a.num_layers * a.num_kv_heads * a.head_dim * octets;
  for (const [lib, val] of [[t("m_couches"), a.num_layers], [t("m_tetes"), a.num_kv_heads], [t("m_dim"), a.head_dim],
    [t("m_ctxmax"), a.context_max ? `${ctxCourt(a.context_max)} tokens` : "–"],
    [t("m_kv_token"), `${f(parToken / 1024, 0)} Kio (${t("m_calcule")})`]]) dl.append(el("dt", null, lib), el("dd", null, String(val)));
  p.append(dl);
  return p;
}

function boitePieces(d) {
  const e = d.estimation_go, b = d.budget_go;
  const p = el("div", "panneau pieces");
  p.append(el("span", "libelle", t("pieces")));
  const dl = el("dl");
  const ligne = (c, lib, val, cl = "") => {
    const dt = el("dt", cl);
    if (c) { const i = el("i"); i.style.background = c; dt.append(i); }
    dt.append(lib);
    dl.append(dt, el("dd", cl, val));
  };
  ligne("var(--a)", t("p_poids"), go(e.poids, 2));
  ligne("var(--b)", t("p_kv"), go(e.cache_kv, 2));
  ligne("#5a6070", t("p_buffers"), go(e.buffers, 2));
  ligne(null, t("p_total"), go(e.total, 2), "total");
  ligne(null, t("p_capacite"), go(b.utilisable, 2));
  const r = b.utilisable - e.total;
  ligne(null, r >= 0 ? t("p_reste") : t("p_manque"), go(Math.abs(r), 2), r >= 0 ? "reste" : "manque");
  p.append(dl);
  const calc = el("details");
  calc.style.marginTop = "12px";
  calc.append(el("summary", null, t("detail_calcul")));
  const c = el("div", "aide");
  c.style.cssText = "display:flex;flex-direction:column;gap:4px;margin-top:8px";
  const ajout = (x) => c.append(el("p", null, x));
  ajout(t("d_budget", go(b.disponible), b.liberable ? t("d_liberable", go(b.liberable)) : "", go(b.marge), go(b.utilisable), go(b.totale)));
  ajout(d.source === "installé" ? t("d_source_installe", go(d.taille_fichier_go, 2), d.format) : t("d_source_registre", go(d.taille_fichier_go, 2), d.format));
  if (d.projecteur_go) ajout(t("d_projecteur", go(d.projecteur_go, 2)));
  if (d.couches_glissantes) ajout(t("d_glissantes", d.couches_glissantes, d.architecture.num_layers));
  ajout(t("d_formule"));
  calc.append(c);
  p.append(calc);
  return p;
}

function zoneActions(d) {
  const zone = el("div", "actions-modele");
  const tient = d.verdict !== "trop_gros";
  if (d.source === "installé") {
    const b = bouton(t("a_discuter_court"), "principal", () => nouvelleConversationAvec(d.modele));
    b.disabled = !tient || !etat.ollama.disponible;
    const m = bouton(t("m_banc"), "", () => { location.hash = `#/bancs?nom=${encodeURIComponent(d.modele)}`; });
    m.disabled = !tient;
    zone.append(b, m);
    if (!tient) zone.append(el("span", "raison", t("raison_grise")));
    return zone;
  }
  const besoin = d.telechargement_go;
  const b = bouton(t("a_telecharger", go(besoin, 2)), "principal", async () => {
    const prog = el("div", "progres-dl");
    const haut = el("div", "haut");
    const tx = el("span", null, t("dl_etat")["pulling manifest"]);
    haut.append(tx);
    const barre = el("div", "barre-progres");
    const r = el("span");
    barre.append(r);
    prog.append(haut, barre);
    zone.replaceChildren(prog);
    addEventListener("beforeunload", avertirQuitter);
    try {
      await telecharger(d.modele, (txt, part) => { tx.textContent = txt; if (part !== null) r.style.width = `${part * 100}%`; });
      removeEventListener("beforeunload", avertirQuitter);
      await chargerOllama();
      nouvelleConversationAvec(d.modele);
    } catch (err) {
      removeEventListener("beforeunload", avertirQuitter);
      zone.replaceChildren(el("p", "erreur-bloc", err.message));
    }
  });
  zone.append(b);
  if (!tient) { b.disabled = true; zone.append(el("span", "raison", t("raison_grise"))); }
  else if (!etat.ollama.disponible) { b.disabled = true; zone.append(el("span", "raison bad", t("a_ollama_absent"))); }
  else if (d.disque_libre_go < besoin * 1.05) { b.disabled = true; zone.append(el("span", "raison bad", t("a_disque", go(besoin, 2), go(d.disque_libre_go, 1)))); }
  return zone;
}

function textePiste(p) { const fab = T().piste[p.type]; return fab ? fab(p, f) : [p.type, ""]; }

function elementOpti(p) {
  const [titre, detail] = textePiste(p);
  const c = el("div", `opti${p.rentre ? " suffit" : ""}`);
  const h = el("p", "titre");
  h.append(titre);
  if (p.gain_go !== undefined) h.append(el("span", `gain${p.rentre ? " ok" : ""}`, `${t("gain", go(p.gain_go, 2), false)} · ${p.rentre ? t("suffit_seul") : t("pas_seul")}`));
  c.append(h, el("p", "detail", detail));
  let action = null;
  if (p.type === "contexte") action = bouton(t("action_appliquer"), "petit", () => { etat.numCtx = p.valeur; garder("edgefit.ctx", String(p.valeur)); reverifier(); });
  else if ((p.type === "variante" && p.nom) || p.type === "petit_modele") {
    action = bouton(t("action_verifier", p.type === "variante" ? p.format : p.nom), "petit", () => { location.hash = `#/modele?nom=${encodeURIComponent(p.nom)}&ctx=${etat.numCtx}&kv=${etat.typeKv}`; });
  }
  if (action) { const a = el("div", "act"); a.append(action); c.append(a); }
  if (p.type === "cache_kv") c.append(pliKv(p.valeur, p.commande));
  if (p.type === "bureau") c.append(pliCommande([el("p", "aide", t("commande_terminal")), blocCommande(p.commande), el("p", "aide", t("bureau_retour")), blocCommande(p.retour)]));
  return c;
}

function blocCombinaisons(d, titre) {
  const bloc = el("section", "optis");
  bloc.style.marginTop = "18px";
  bloc.append(el("h3", null, titre));
  for (const p of d.piles.filter((x) => x.cle !== "tel_quel")) {
    const c = el("div", "opti suffit");
    const tete = el("p", "titre");
    tete.append(t("pile_tient", p.format), el("span", "aide", `(${T().pile[p.cle] || p.cle})`));
    if (p.qualite_degradee) tete.append(el("span", "pastille juste", t("pile_degradee")));
    if (p.telechargement) tete.append(etiq(p.nom ? t("pile_telechargement") : t("pile_tag")));
    if (p.reglage_serveur) tete.append(etiq(t("pile_serveur")));
    const detail = el("p", "detail");
    detail.append(el("span", "mono", t("pile_reglage", p.format, p.num_ctx, p.type_kv)), " · ",
      t("pile_chiffres", go(p.total_go, 2), go(p.reste_go, 2), p.debit_max_tokens_s ? f(p.debit_max_tokens_s, 0) : null));
    c.append(tete, detail);
    if (!p.telechargement || p.nom) {
      const a = el("div", "act");
      a.append(bouton(t("pile_choisir"), "petit", () => {
        etat.numCtx = p.num_ctx;
        if (p.reglage_serveur) { etat.typeKv = p.type_kv; garder("edgefit.kv", p.type_kv); }
        location.hash = `#/modele?nom=${encodeURIComponent(p.telechargement && p.nom ? p.nom : d.modele)}&ctx=${etat.numCtx}&kv=${etat.typeKv}`;
      }));
      c.append(a);
    }
    if (p.reglage_serveur) c.append(pliKv(p.type_kv));
    bloc.append(c);
  }
  return bloc;
}

function listeOptimisations(d) {
  const nb = d.solutions.categories.reduce((n, c) => n + c.pistes.length, 0);
  const bloc = el("section", "optis");
  bloc.style.marginTop = "22px";
  const combis = d.piles.filter((x) => x.cle !== "tel_quel");
  bloc.append(titrePage(t("opti_titre", nb), t("opti_sous", combis.length > 0)));
  if (combis.length) bloc.append(blocCombinaisons(d, t("combi_titre")));
  for (const cat of d.solutions.categories) {
    const h = el("h3", null, T().categorie[cat.cle] || cat.cle);
    h.append(el("span", "n", String(cat.pistes.length)));
    const suffit = cat.pistes.some((p) => p.rentre);
    if (cat.cle === "materiel" || cat.cle === "info" || (!suffit && cat.cle !== "modele")) {
      const pli = el("details", "pli-cat");
      const resume = el("summary");
      resume.append(h);
      pli.append(resume, ...cat.pistes.map(elementOpti));
      bloc.append(pli);
    } else bloc.append(h, ...cat.pistes.map(elementOpti));
  }
  return bloc;
}

// --- page Chat ------------------------------------------------------------------------------

async function pageChat(id) {
  const zone = $("page");
  zone.className = "page-chat";
  const ch = etat.chat = { conv: null, modele: null, messages: [], vitesses: [], derniere: null, estimation: null, ctxUtilise: 0, rail: true, envoi: false };
  if (id) {
    try {
      ch.conv = await json(`/api/conversations/${id}`);
    } catch { location.replace("#/chat"); return; }
    ch.modele = ch.conv.modele;
    ch.messages = ch.conv.messages.map((m) => ({ role: m.role, content: m.contenu, mesures: m.mesures }));
    for (const m of ch.messages) if (m.mesures) { ch.vitesses.push(m.mesures.debit_tokens_s || 0); ch.derniere = m.mesures; ch.ctxUtilise += (m.mesures.tokens_prompt || 0) + (m.mesures.tokens_generes || 0); }
    $("titre-mobile").textContent = ch.conv.titre || t("conv_sans_titre");
  } else {
    ch.modele = etat.installes.find((m) => m.nom === pref("edgefit.dernier", ""))?.nom || etat.installes[0]?.nom || null;
  }
  const numCtx = ch.conv?.num_ctx || etat.numCtx;

  const colonne = el("div", "chat-colonne");
  const barre = el("div", "chat-barre");
  const choix = el("select");
  choix.setAttribute("aria-label", t("recherche_label"));
  for (const m of etat.installes) choix.append(new Option(`${m.nom} · ${m.quantification || ""}`, m.nom));
  if (ch.modele) choix.value = ch.modele;
  choix.disabled = !etat.installes.length;
  choix.addEventListener("change", async () => {
    ch.modele = choix.value;
    garder("edgefit.dernier", ch.modele);
    if (ch.conv) await fetch(`/api/conversations/${ch.conv.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ modele: ch.modele }) });
    ch.estimation = null;
    majEtatModele(); rendreMessages(); estimerChat(); majRail();
  });
  const etatModele = el("span", "etat-modele");
  etatModele.id = "etat-modele";
  const bReglages = bouton("", "petit icone-texte", () => { panneauReglages.hidden = !panneauReglages.hidden; });
  bReglages.append(icone("reglages"), el("span", "txt", t("chat_reglages")));
  bReglages.setAttribute("aria-label", t("chat_reglages"));
  const bRail = bouton(t("chat_mesures"), "petit", () => {
    const rail = $("rail");
    if (matchMedia("(max-width: 1100px)").matches) rail.classList.toggle("ouvert");
    else { ch.rail = !ch.rail; zone.classList.toggle("sans-rail", !ch.rail); }
  });
  bRail.textContent = "";
  bRail.className = "bouton petit icone-texte";
  bRail.append(icone("mesures"), el("span", "txt", t("chat_mesures")));
  bRail.setAttribute("aria-label", t("chat_mesures"));
  barre.append(choix, etatModele, el("span", "espace"), bReglages, bRail);

  // réglages de la conversation : instructions, température, contexte
  const panneauReglages = el("div", "reglages-chat");
  panneauReglages.hidden = true;
  const systeme = el("textarea");
  systeme.placeholder = t("chat_systeme_aide");
  systeme.value = ch.conv?.systeme || "";
  const temp = el("input");
  temp.type = "number"; temp.min = "0"; temp.max = "2"; temp.step = "0.1";
  temp.value = ch.conv?.temperature ?? "";
  temp.placeholder = "0,8";
  const droite = el("div", "groupe-champs");
  droite.style.flexDirection = "column";
  droite.style.alignItems = "stretch";
  droite.append(champ(t("chat_temperature"), temp), champ(t("contexte"), selectContexte(numCtx, null, async (v) => {
    if (ch.conv) ch.conv = await json(`/api/conversations/${ch.conv.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ num_ctx: v }) });
    else etat.numCtx = v;
    ch.estimation = null; estimerChat(); majContexte();
  })));
  const enregistrer = async () => {
    if (!ch.conv) return;
    const tval = temp.value === "" ? null : Math.min(2, Math.max(0, Number(temp.value)));
    ch.conv = await json(`/api/conversations/${ch.conv.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ systeme: systeme.value, temperature: tval }) });
  };
  systeme.addEventListener("change", enregistrer);
  temp.addEventListener("change", enregistrer);
  panneauReglages.append(champ(t("chat_systeme"), systeme), droite);

  const messages = el("div", "messages");
  messages.id = "messages";
  const col = el("div", "messages-col");
  col.id = "messages-col";
  messages.append(col);

  const zoneComposer = el("div", "composer-zone");
  const cc = el("div", "composer-col");
  const ctxLigne = el("div", "contexte-ligne");
  ctxLigne.id = "contexte-ligne";
  const form = el("form", "composer");
  const champTexte = el("textarea");
  champTexte.rows = 1;
  champTexte.placeholder = t("message_placeholder");
  champTexte.setAttribute("aria-label", t("message_label"));
  const envoyer = el("button", "bouton principal");
  envoyer.type = "submit";
  envoyer.append(icone("envoyer"));
  envoyer.setAttribute("aria-label", t("envoyer"));
  form.append(champTexte, envoyer);
  cc.append(ctxLigne, form, el("p", "aide", t("entree_aide")));
  zoneComposer.append(cc);
  colonne.append(barre, panneauReglages, messages, zoneComposer);

  const rail = el("aside", "rail");
  rail.id = "rail";
  zone.replaceChildren(colonne, rail);

  champTexte.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });
  champTexte.addEventListener("input", () => { champTexte.style.height = "auto"; champTexte.style.height = `${Math.min(champTexte.scrollHeight, 160)}px`; });
  form.addEventListener("submit", (e) => { e.preventDefault(); envoyerMessage(champTexte, envoyer, systeme, temp); });

  rendreMessages();
  majEtatModele();
  majContexte();
  majRail();
  estimerChat();
  if (!etat.installes.length) envoyer.disabled = true;
  champTexte.focus();
}

function majEtatModele() {
  const ch = etat.chat;
  const e = $("etat-modele");
  if (!e || !ch) return;
  const charge = etat.charges.find((c) => c.nom === ch.modele);
  e.replaceChildren(el("span", `point ${charge ? "ok" : ""}`), charge ? t("chat_charge", go(charge.taille_go, 2)) : t("chat_non_charge"));
}

async function estimerChat() {
  const ch = etat.chat;
  if (!ch?.modele) return;
  const numCtx = ch.conv?.num_ctx || etat.numCtx;
  try {
    ch.estimation = await json("/api/compatibilite", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ modele: ch.modele, num_ctx: numCtx, type_kv: etat.typeKv }) });
  } catch { ch.estimation = null; }
  if (!ch.messages.length) rendreMessages();
  majRail();
}

function rendreMessages() {
  const ch = etat.chat;
  const col = $("messages-col");
  col.replaceChildren();
  if (!ch.messages.length) {
    const a = el("div", "accueil-chat");
    a.append(el("h2", null, t("chat_bienvenue")), el("p", null, etat.installes.length ? t("chat_bienvenue_sous") : t("chat_aucun_modele")));
    if (!etat.installes.length) { const l = el("a", "bouton principal", t("nav_decouvrir")); l.href = "#/decouvrir"; a.append(l); col.append(a); return; }
    const installe = etat.installes.find((m) => m.nom === ch.modele);
    const fiche = el("section", "panneau fiche");
    fiche.append(el("h3", "mono titre-fiche", ch.modele || "–"));
    const dl = el("dl", "lignes-mesure");
    const ligne = (lib, val) => dl.append(el("dt", null, lib), el("dd", null, val));
    if (installe) ligne(t("s_format"), [installe.parametres, installe.quantification].filter(Boolean).join(" · "));
    const charge = etat.charges.find((c) => c.nom === ch.modele);
    ligne(t("mm_etat"), charge ? t("chat_charge", go(charge.taille_go, 2)) : t("chat_non_charge"));
    if (ch.estimation) ligne(t("rail_memoire"), `${go(ch.estimation.estimation_go.total, 2)} (${t("chat_estime")})`);
    const banc = dernierBanc(ch.modele);
    ligne(t("mm_vitesse"), banc ? `${f(banc.mesures.debit_tokens_s, 1)} tok/s · ${t("rail_score")} ${banc.score.score}` : t("non_mesure"));
    fiche.append(dl);
    const tags = etat.catalogue?.modeles.find((m) => m.nom === ch.modele)?.tags || ["chat"];
    const idees = [...new Set(tags.flatMap((tg) => T().suggestions[tg] || []).concat(T().suggestions.chat))].slice(0, 4);
    const sugg = el("div", "suggestions");
    sugg.append(el("span", "libelle", t("chat_suggestions")));
    for (const q of idees) {
      const b = bouton(q, "suggestion", () => { const c = document.querySelector(".composer textarea"); c.value = q; c.focus(); c.dispatchEvent(new Event("input")); });
      b.className = "suggestion";
      sugg.append(b);
    }
    a.append(fiche, sugg);
    const recentes = etat.conversations.filter((c) => c.modele === ch.modele).slice(0, 3);
    if (recentes.length) {
      const r = el("div", "suggestions");
      r.append(el("span", "libelle", t("chat_recentes")));
      for (const c of recentes) { const l = el("a", "suggestion", c.titre || t("conv_sans_titre")); l.href = `#/chat?id=${c.id}`; r.append(l); }
      a.append(r);
    }
    col.append(a);
    return;
  }
  for (const m of ch.messages) {
    if (m.role === "user") col.append(el("div", "msg-user", m.content));
    else col.append(blocReponse(m.content, m.mesures, ch.conv?.modele || ch.modele));
  }
  defiler();
}

function blocReponse(texte, mesures, modele) {
  const b = el("div", "msg-ia");
  const auteur = el("div", "auteur");
  auteur.append(el("i"), modele || "");
  const tx = el("div", "texte", texte);
  b.append(auteur, tx);
  if (mesures) b.append(anatomie(mesures));
  const outils = el("div", "msg-outils");
  const copier = el("button", null, t("copier"));
  copier.type = "button";
  copier.addEventListener("click", async () => { try { await navigator.clipboard.writeText(tx.textContent); copier.textContent = t("copie"); } catch { /* http : pas de presse-papier */ } });
  outils.append(copier);
  b.append(outils);
  return b;
}

// anatomie d'une réponse : chargement | lecture du message | génération, à l'échelle
function anatomie(m) {
  const bloc = el("div", "anatomie");
  const generation = Math.max(0, m.duree_totale_s - m.ttft_mesure_s);
  const lecture = m.prefill_s || 0;
  const chargement = m.chargement_s > 0.05 ? m.chargement_s : 0;
  const total = Math.max(chargement + lecture + generation, 0.001);
  const barre = el("div", "anatomie-barre");
  barre.setAttribute("aria-hidden", "true");
  for (const [cl, v] of [["ch", chargement], ["lu", lecture], ["ge", generation]]) {
    if (v <= 0) continue;
    const s = el("span", cl);
    s.style.flex = `${v / total}`;
    barre.append(s);
  }
  const leg = el("div", "anatomie-legende");
  const item = (couleur, lib, v) => { const s = el("span"); const i = el("i"); i.style.background = couleur; s.append(i, `${lib} ${f(v, 2)} s`); leg.append(s); };
  if (chargement) item("#5a6070", t("an_chargement"), chargement);
  item("var(--a)", t("an_lecture"), lecture);
  item("var(--b)", t("an_generation"), generation);
  const recu = el("p", "recu");
  for (const [avant, fort, apres] of T().recu(m, f)) recu.append(avant, el("strong", null, fort), apres);
  if (chargement) recu.append(el("span", "froid", t("an_froid")));
  bloc.append(barre, leg, recu);
  return bloc;
}

function defiler() { const m = $("messages"); if (m) m.scrollTop = m.scrollHeight; }

function majContexte() {
  const ch = etat.chat;
  const z = $("contexte-ligne");
  if (!z || !ch) return;
  const total = ch.conv?.num_ctx || etat.numCtx;
  const part = Math.min(1, ch.ctxUtilise / total);
  const barre = el("div", "barre-progres");
  const s = el("span");
  s.style.width = `${part * 100}%`;
  barre.append(s);
  z.className = `contexte-ligne${part > 0.9 ? " alerte" : ""}`;
  z.replaceChildren(el("span", null, t("chat_contexte_sat", f(ch.ctxUtilise, 0), f(total, 0))), barre);
  if (part > 0.9) z.append(el("span", null, t("chat_contexte_plein")));
}

async function envoyerMessage(champTexte, bEnvoyer, systeme, temp) {
  const ch = etat.chat;
  const question = champTexte.value.trim();
  if (!question || !ch.modele || ch.envoi) return;
  ch.envoi = true;
  bEnvoyer.disabled = true;
  champTexte.value = "";
  champTexte.style.height = "auto";
  if (!ch.conv) {
    try {
      const tval = temp.value === "" ? null : Number(temp.value);
      ch.conv = await json("/api/conversations", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ modele: ch.modele, systeme: systeme.value, temperature: tval, num_ctx: etat.numCtx }) });
      history.replaceState(null, "", `#/chat?id=${ch.conv.id}`);
    } catch (err) { ch.envoi = false; bEnvoyer.disabled = false; alert(err.message); return; }
  }
  if (!ch.messages.length) $("messages-col").replaceChildren();
  ch.messages.push({ role: "user", content: question });
  $("messages-col").append(el("div", "msg-user", question));
  const bloc = blocReponse("", null, ch.modele);
  bloc.classList.add("attente");
  $("messages-col").append(bloc);
  const tx = bloc.querySelector(".texte");
  defiler();
  let reponse = "", tokens = 0, premier = null;
  try {
    const r = await poster("/api/chat", { modele: ch.modele, conversation_id: ch.conv.id, num_ctx: ch.conv.num_ctx,
      systeme: ch.conv.systeme || "", temperature: ch.conv.temperature, messages: ch.messages.map(({ role, content }) => ({ role, content })) });
    if (!r.ok) throw new Error(await messageErreur(r));
    for await (const evt of ndjson(r)) {
      if (evt.type === "token") {
        reponse += evt.texte;
        tx.textContent = reponse;
        tokens += 1;
        premier ??= performance.now();
        const ecoule = (performance.now() - premier) / 1000;
        if (ecoule > 0.3) majVitesseDirect(tokens / ecoule);
        defiler();
      } else if (evt.type === "erreur") {
        throw new Error(evt.erreur);
      } else if (evt.type === "fin") {
        const m = evt.metriques;
        bloc.classList.remove("attente");
        bloc.insertBefore(anatomie(m), bloc.querySelector(".msg-outils"));
        ch.messages.push({ role: "assistant", content: reponse, mesures: m });
        ch.vitesses.push(m.debit_tokens_s || 0);
        ch.derniere = m;
        ch.ctxUtilise += (m.tokens_prompt || 0) + (m.tokens_generes || 0);
        try { etat.charges = await json("/api/charges"); } catch { /* sans importance */ }
        majEtatModele(); majContexte(); majRail(); majRam(); defiler();
      }
    }
  } catch (err) {
    ch.messages.pop();
    bloc.classList.remove("attente");
    bloc.classList.add("erreur");
    tx.textContent = err.message;
  } finally {
    ch.envoi = false;
    bEnvoyer.disabled = false;
    champTexte.focus();
    chargerConversations();
  }
}

function bandeVitesse(v) { return v < 5 ? "lent" : v < 15 ? "lecture" : v < 40 ? "confort" : "instant"; }

function majVitesseDirect(v) {
  const n = $("vitesse-direct");
  if (n) n.textContent = f(v, 1);
  const j = $("jauge-vitesse");
  if (j) j.style.width = `${Math.min(100, (v / 40) * 100)}%`;
}

function majRail() {
  const ch = etat.chat;
  const rail = $("rail");
  if (!rail || !ch) return;
  const m = ch.derniere;
  const blocs = [];

  const v = m?.debit_tokens_s;
  const b1 = el("section", "rail-bloc");
  const tete = el("div", "grand-nombre");
  const n = el("strong", null, v ? f(v, 1) : "–");
  n.id = "vitesse-direct";
  tete.append(n, el("span", null, "tok/s"));
  if (v) tete.append(el("span", `bande ${bandeVitesse(v)}`, T().bandes[bandeVitesse(v)]));
  const jauge = el("div", "jauge-lecture");
  const rempli = el("span");
  rempli.id = "jauge-vitesse";
  rempli.style.width = `${Math.min(100, ((v || 0) / 40) * 100)}%`;
  const repere = el("span", "repere");
  repere.style.left = `${(5 / 40) * 100}%`;
  jauge.append(rempli, repere);
  const rt = el("div", "repere-texte");
  rt.append(el("span", null, t("rail_lecture")), el("span", null, v ? t("rail_fois", f(v / 5, 1)) : ""));
  b1.append(el("span", "libelle", t("rail_vitesse")), tete, jauge, rt);
  if (ch.vitesses.length > 1) b1.append(courbe(ch.vitesses));
  blocs.push(b1);

  if (m) {
    const b2 = el("section", "rail-bloc");
    const ch1 = m.chargement_s > 0.05 ? m.chargement_s : 0;
    const premierTok = Math.max(0, m.ttft_mesure_s - ch1 - (m.prefill_s || 0));
    const g = el("div", "grand-nombre");
    g.append(el("strong", null, f(m.ttft_mesure_s, 2)), el("span", null, "s"));
    const dl = el("dl", "lignes-mesure");
    for (const [lib, val] of [[t("rail_chargement"), ch1], [t("rail_lu"), m.prefill_s || 0], [t("rail_reste"), premierTok]]) dl.append(el("dt", null, lib), el("dd", null, `${f(val, 2)} s`));
    b2.append(el("span", "libelle", t("rail_premier")), g, dl);
    const b3 = el("section", "rail-bloc");
    const dl3 = el("dl", "lignes-mesure");
    dl3.append(el("dt", null, t("rail_prefill")), el("dd", null, m.debit_prefill_tokens_s ? `${f(m.debit_prefill_tokens_s, 0)} tok/s` : "–"));
    b3.append(dl3);
    blocs.push(b2, b3);
  } else {
    blocs.push(el("p", "aide", t("rail_attente")));
  }

  const b4 = el("section", "rail-bloc");
  const dl4 = el("dl", "lignes-mesure");
  const est = ch.estimation?.estimation_go?.total;
  dl4.append(el("dt", null, t("rail_estimee")), el("dd", null, est ? go(est, 2) : "–"));
  const mes = m?.memoire_chargee_go;
  const ecart = mes && est ? ` (${mes > est ? "+" : ""}${f(((mes - est) / est) * 100, 0)} %)` : "";
  dl4.append(el("dt", null, t("rail_mesuree")), el("dd", null, mes ? `${go(mes, 2)}${ecart}` : "–"));
  const quatre = el("div", "quatre");
  quatre.id = "rail-materiel";
  b4.append(el("span", "libelle", t("rail_memoire")), dl4, quatre);
  blocs.push(b4);

  const mesures = ch.messages.map((x) => x.mesures).filter(Boolean);
  if (mesures.length > 1) {
    const st = el("section", "rail-bloc");
    const dls = el("dl", "lignes-mesure");
    const moy = mesures.reduce((a, x) => a + (x.debit_tokens_s || 0), 0) / mesures.length;
    const ttfts = mesures.map((x) => x.ttft_mesure_s).sort((a, b) => a - b);
    const tokens = mesures.reduce((a, x) => a + (x.tokens_prompt || 0) + (x.tokens_generes || 0), 0);
    const froids = mesures.filter((x) => x.chargement_s > 0.05).length;
    for (const [lib, val] of [[t("stats_moyenne"), `${f(moy, 1)} tok/s`], [t("stats_ttft"), `${f(ttfts[Math.floor(ttfts.length / 2)], 2)} s`],
      [t("stats_tokens"), f(tokens, 0)], [t("stats_froids"), String(froids)]]) dls.append(el("dt", null, lib), el("dd", null, val));
    st.append(el("span", "libelle", t("stats_titre")), dls);
    blocs.push(st);
  }

  const b5 = el("section", "rail-bloc");
  b5.append(el("span", "libelle", t("rail_score")));
  const banc = dernierBanc(ch.modele);
  if (banc) {
    const ligne = el("div", "grand-nombre");
    ligne.append(anneau(banc.score.score), el("span", "aide", t("b_phrase", banc.score, banc.mesures, f)));
    b5.append(ligne, sousScores(banc.score));
  } else {
    b5.append(el("p", "aide", t("rail_pas_score")));
    const l = el("a", "bouton petit", t("m_banc"));
    l.href = `#/bancs?nom=${encodeURIComponent(ch.modele || "")}`;
    b5.append(l);
  }
  blocs.push(b5);
  rail.replaceChildren(...blocs);
  majRailMateriel();
}

function sousScores(s) {
  const z = el("div", "sous-scores");
  for (const [lib, v] of [[t("b_colonnes")[2], s.vitesse], [t("b_colonnes")[3], s.reactivite]]) {
    const l = el("div");
    const barre = el("div", "barre-progres");
    const r = el("span");
    r.style.width = `${v}%`;
    barre.append(r);
    l.append(el("span", null, lib), barre, el("b", null, String(v)));
    z.append(l);
  }
  return z;
}

function majRailMateriel() {
  const z = $("rail-materiel");
  const m = etat.metrique;
  if (!z || !m) return;
  const cases = [["CPU", m.cpu_pct, "%", 0], ["GPU", m.gpu_pct, "%", 0], ["°C", m.temp_cpu_c, "", 0], ["W", m.puissance_w, "", 1]];
  z.replaceChildren(...cases.map(([lib, v, u, d]) => { const c = el("div"); c.append(el("strong", null, v === null || v === undefined ? "–" : `${f(v, d)}${u}`), el("span", null, lib)); return c; }));
}

// --- page Mes modèles ---------------------------------------------------------------------

function duree(s) {
  if (!isFinite(s) || s <= 0) return "0 s";
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  return `${f(s / 3600, 1)} h`;
}
function dateCourte(d) {
  return new Date(d).toLocaleString(etat.langue === "fr" ? "fr-FR" : "en-US", { dateStyle: "short", timeStyle: "short" });
}
function barreStockage(parts, total) {
  const b = el("div", "barre-ram");
  b.style.height = "8px";
  for (const [classe, v] of parts) { const s = el("span", classe); s.style.width = `${(v / total) * 100}%`; b.append(s); }
  return b;
}

async function pageModeles() {
  const zone = $("page");
  zone.className = "page";
  await Promise.all([chargerOllama(), chargerBancs(), chargerConversations()]);
  const tete = titrePage(t("mm_titre"), t("mm_sous"));
  if (!etat.installes.length) {
    const vide = el("section", "panneau vide-grand");
    const l = el("a", "bouton principal", t("nav_decouvrir"));
    l.href = "#/decouvrir";
    vide.append(el("h2", null, t("mm_vide_titre")), el("p", "aide", t("mm_vide_texte")), l);
    zone.replaceChildren(tete, vide);
    return;
  }
  const disque = etat.ollama.disque;
  const totalModeles = etat.installes.reduce((s, m) => s + m.taille_go, 0);
  const stock = el("section", "panneau bandeau-stockage");
  if (disque) {
    const p = el("p", disque.libre_go < 10 ? "raison bad" : "aide", t("mm_stockage", go(totalModeles, 2), go(disque.libre_go, 1), disque.dossier));
    stock.append(p, barreStockage([["mod", totalModeles], ["sys", Math.max(0, disque.total_go - disque.libre_go - totalModeles)]], disque.total_go));
  }
  const table = el("table", "tableau");
  const tr = el("tr");
  for (const [lib, cl] of [[t("mm_modele")], [t("mm_etat")], [t("mm_vitesse"), "num"], [t("mm_utilise"), "num cache-mobile"], [t("mm_taille"), "num cache-mobile"], [""]]) tr.append(el("th", cl || "", lib));
  const thead = el("thead");
  thead.append(tr);
  const tbody = el("tbody");
  const decomptes = [];
  for (const m of etat.installes) {
    const charge = etat.charges.find((c) => c.nom === m.nom);
    const banc = dernierBanc(m.nom);
    const derniere = etat.conversations.filter((c) => c.modele === m.nom).sort((a, b) => b.modifie - a.modifie)[0];
    const ligne = el("tr");
    const nom = el("td");
    nom.className = "col-modele";
    nom.append(el("div", "mono", m.nom), el("div", "aide", [m.parametres, m.quantification].filter(Boolean).join(" · ")));
    if (m.modifie) nom.title = `${t("mm_installe_le")} ${dateCourte(m.modifie)}`;
    const etatC = el("td", "col-etat");
    if (charge) {
      etatC.append(el("div", "charge-oui", `${t("mm_charge")} · ${go(charge.taille_go, 2)}`));
      const details = el("div", "aide");
      const part = charge.taille ? Math.round((charge.taille_vram / charge.taille) * 100) : null;
      const morceaux = [];
      if (part !== null && etat.metrique?.gpu_pct !== null) morceaux.push(t("mm_gpu", part));
      if (charge.num_ctx) morceaux.push(t("mm_ctx", ctxCourt(charge.num_ctx)));
      details.textContent = morceaux.join(" · ");
      etatC.append(details);
      if (charge.expire) {
        const dec = el("div", "aide");
        decomptes.push([dec, new Date(charge.expire).getTime()]);
        etatC.append(dec);
      }
    } else etatC.append(el("span", "aide", t("mm_repos")));
    const vit = el("td", "num");
    if (banc) { const z = el("div", "score-ligne"); z.append(el("span", null, `${f(banc.mesures.debit_tokens_s, 1)} tok/s`), anneau(banc.score.score)); vit.append(z); }
    else { const l = el("a", "lien-discret", t("non_mesure")); l.href = `#/bancs?nom=${encodeURIComponent(m.nom)}`; vit.append(l); }
    const acts = el("div", "acts");
    acts.append(bouton(t("a_discuter_court"), "petit principal", () => nouvelleConversationAvec(m.nom)),
      bouton(t("mm_mesurer"), "petit", () => { location.hash = `#/bancs?nom=${encodeURIComponent(m.nom)}`; }));
    if (charge) acts.append(bouton(t("mm_decharger"), "petit", async () => {
      const r = await poster("/api/modeles/decharger", { nom: m.nom });
      if (!r.ok) alert(await messageErreur(r));
      pageModeles();
    }));
    acts.append(bouton(t("mm_supprimer"), "petit danger", async () => {
      if (!(await confirmer(t("mm_confirmer", m.nom), t("mm_confirmer_texte", go(m.taille_go, 2)), t("mm_supprimer")))) return;
      const r = await poster("/api/modeles/supprimer", { nom: m.nom });
      if (!r.ok) alert(await messageErreur(r));
      pageModeles();
    }));
    const tdActs = el("td");
    tdActs.append(acts);
    ligne.append(nom, etatC, vit, el("td", "num cache-mobile", derniere ? dateCourte(derniere.modifie * 1000) : t("jamais")),
      el("td", "num cache-mobile", go(m.taille_go, 2)), tdActs);
    tbody.append(ligne);
  }
  table.append(thead, tbody);
  zone.replaceChildren(tete, stock, table);
  // compte à rebours avant qu'Ollama ne libère un modèle chargé (keep_alive)
  const tic = () => {
    if (etat.page !== "modeles") return;
    for (const [e, fin] of decomptes) e.textContent = t("mm_libere_dans", duree((fin - Date.now()) / 1000));
    if (decomptes.length) setTimeout(tic, 1000);
  };
  tic();
}

// --- page Bancs d'essai --------------------------------------------------------------------

function derniersParModele() {
  const vus = new Map();
  for (const b of etat.bancs) if (b.score && !vus.has(b.modele)) vus.set(b.modele, b);
  return [...vus.values()];
}

async function pageBancs() {
  const zone = $("page");
  zone.className = "page";
  await Promise.all([chargerOllama(), chargerBancs()]);
  let catalogue = etat.catalogue;
  if (!catalogue) { try { catalogue = etat.catalogue = await json(`/api/catalogue?num_ctx=${etat.numCtx}&type_kv=${etat.typeKv}`); } catch { catalogue = null; } }
  const tete = titrePage(t("b_titre"), t("b_sous"));
  const lancer = el("div", "lancer-banc");
  const choix = el("select");
  for (const m of etat.installes) choix.append(new Option(m.nom, m.nom));
  const demande = route().params.nom;
  if (demande && etat.installes.some((m) => m.nom === demande)) choix.value = demande;
  const phases = el("div", "phases");
  const bLancer = bouton(t("b_lancer"), "principal", async () => { await lancerBanc(choix.value, phases); pageBancs(); });
  bLancer.disabled = !etat.installes.length;
  lancer.append(champ(t("mm_modele"), choix), bLancer, el("span", "aide", t("b_duree")));

  const contenu = [tete, lancer, phases];
  const nonMesures = etat.installes.filter((m) => !dernierBanc(m.nom));
  if (nonMesures.length) {
    const p = el("section", "panneau bandeau-stockage");
    const ligne = el("div", "etiqs");
    ligne.append(...nonMesures.map((m) => etiq(m.nom)));
    const tout = bouton(t("b_tout_mesurer", nonMesures.length), "", async () => {
      tout.disabled = bLancer.disabled = true;
      for (const [i, m] of nonMesures.entries()) {
        const ok = await lancerBanc(m.nom, phases, (ph) => t("b_progression", i + 1, nonMesures.length, m.nom, ph));
        if (!ok) break;
      }
      pageBancs();
    });
    p.append(el("span", "libelle", t("b_non_mesures")), ligne, tout);
    contenu.push(p);
  }
  const derniers = derniersParModele();
  if (!derniers.length) contenu.push(el("p", "panneau explication", t("b_vide")));
  else {
    // graphique : une barre par modèle (dernier essai), triable
    const graph = el("section", "panneau");
    const entete = el("div", "section-titre");
    entete.style.margin = "0 0 12px";
    const tri = el("select");
    for (const [k, lib] of Object.entries(T().b_tri)) tri.append(new Option(`${t("trier")} : ${lib}`, k));
    tri.value = etat.triBancs || "score";
    entete.append(el("span", "libelle", t("b_graphique")), tri);
    const barres = el("div", "barres");
    const dessiner = () => {
      const cle = { score: (b) => b.score.score, vitesse: (b) => b.mesures.debit_tokens_s || 0, energie: (b) => b.mesures.tokens_par_joule || 0 }[tri.value];
      const unite = { score: "/100", vitesse: " tok/s", energie: " tok/J" }[tri.value];
      const tries = derniers.slice().sort((a, b) => cle(b) - cle(a));
      const max = Math.max(...tries.map(cle), 1);
      barres.replaceChildren(...tries.map((b) => {
        const l = el("div", "barre-ligne");
        const piste = el("div", "barre-progres");
        const r = el("span");
        r.style.width = `${(cle(b) / max) * 100}%`;
        piste.append(r);
        l.append(el("span", "mono", b.modele), piste, el("b", null, `${f(cle(b), tri.value === "score" ? 0 : 1)}${unite}`));
        return l;
      }));
    };
    tri.addEventListener("change", () => { etat.triBancs = tri.value; dessiner(); });
    dessiner();
    graph.append(entete, barres);
    contenu.push(graph);

    const table = el("table", "tableau");
    const tr = el("tr");
    t("b_colonnes").forEach((lib, i) => tr.append(el("th", i >= 2 ? `num${i >= 4 ? " cache-mobile" : ""}` : "", lib)));
    const thead = el("thead");
    thead.append(tr);
    const tbody = el("tbody");
    for (const b of derniers) {
      const m = b.mesures;
      const ligne = el("tr");
      const nom = el("td");
      nom.append(el("div", "mono", b.modele), el("div", "aide", t("b_phrase", b.score, m, f)));
      const plafond = catalogue?.modeles.find((x) => x.nom === b.modele)?.plafond_tokens_s;
      if (plafond && m.debit_tokens_s) nom.append(el("div", "aide", t("b_plafond", f(plafond, 0), f(Math.min(100, (m.debit_tokens_s / plafond) * 100), 0))));
      const essais = etat.bancs.filter((x) => x.modele === b.modele).length;
      if (essais > 1) nom.append(el("div", "aide", t("b_essais", essais)));
      const sc = el("td");
      sc.append(anneau(b.score.score));
      const cell = (v, cl) => el("td", cl, v);
      ligne.append(nom, sc, cell(m.debit_tokens_s ? `${f(m.debit_tokens_s, 1)} tok/s` : "–", "num"),
        cell(m.ttft_s ? `${f(m.ttft_s, 2)} s` : "–", "num"), cell(m.chargement_s ? `${f(m.chargement_s, 1)} s` : "–", "num cache-mobile"),
        cell(m.memoire_go ? go(m.memoire_go, 2) : "–", "num cache-mobile"),
        cell(m.tokens_par_joule ? `${f(m.tokens_par_joule, 1)} tok/J` : "–", "num cache-mobile"),
        cell(dateCourte(b.cree * 1000), "num cache-mobile"));
      tbody.append(ligne);
    }
    table.append(thead, tbody);
    contenu.push(table);
  }
  const calc = el("details", "panneau");
  calc.append(el("summary", null, t("b_calcul_titre")), ...t("b_calcul").map((x) => { const p = el("p", "explication", x); p.style.marginTop = "6px"; return p; }));
  contenu.push(calc);
  zone.replaceChildren(...contenu);
}

async function lancerBanc(nom, zone, etiquette = null) {
  const etapes = ["dechargement", "froid", "chaud", "fin"];
  const rendre = (encours) => {
    if (etiquette) { zone.replaceChildren(el("span", "encours", etiquette(T().b_phases[encours]))); return; }
    zone.replaceChildren(...etapes.map((e, i) => el("span", i < etapes.indexOf(encours) ? "fait" : e === encours ? "encours" : "", T().b_phases[e])));
  };
  rendre("dechargement");
  try {
    const r = await poster("/api/banc", { nom });
    if (!r.ok) throw new Error(await messageErreur(r));
    for await (const evt of ndjson(r)) {
      if (evt.type === "phase") rendre(evt.phase);
      else if (evt.type === "erreur") throw new Error(evt.erreur);
      else if (evt.type === "fin") { rendre("fin"); await Promise.all([chargerOllama(), chargerBancs()]); return true; }
    }
  } catch (err) {
    zone.replaceChildren(el("p", "erreur-bloc", err.message));
  }
  return false;
}

// --- page Matériel --------------------------------------------------------------------------

async function chargerDiagnostic(forcer = false) {
  if (etat.diagnostic && !forcer) return etat.diagnostic;
  try { etat.diagnostic = await json("/api/diagnostic"); } catch { etat.diagnostic = null; }
  return etat.diagnostic;
}

function pageMateriel() {
  const zone = $("page");
  zone.className = "page";
  zone.replaceChildren(titrePage(t("mat_titre"), t("mat_sous")));
  rendreMateriel();
  chargerDiagnostic().then(() => rendreMateriel());
}

function rendreMateriel() {
  const zone = $("page");
  const m = etat.metrique;
  if (!m || etat.page !== "materiel") return;
  const contenu = [zone.firstChild];
  if (m.alertes.length) contenu.push(...m.alertes.map((a) => el("p", "alerte-bloc", a)));

  const haut = el("div", "modele-grille");
  // identité de la carte
  const id = el("section", "panneau");
  id.append(el("span", "libelle", t("mat_identite")));
  const d = etat.diagnostic?.faits;
  if (!d) id.append(el("p", "aide", t("mat_chargement")));
  else {
    const dl = el("dl", "lignes-mesure");
    dl.style.marginTop = "10px";
    const ligne = (lib, val) => { if (val) dl.append(el("dt", null, lib), el("dd", null, val)); };
    ligne(t("mat_carte"), d.carte || m.machine);
    ligne(t("mat_jetpack"), d.jetpack ? `${d.jetpack} · ${d.l4t}` : null);
    ligne(t("mat_mode"), d.mode);
    ligne(t("mat_ollama"), d.ollama_version);
    ligne(t("mat_disque"), `${go(d.disque_libre_go, 1)} / ${go(d.disque_total_go, 0)}`);
    ligne(t("mat_demarrage"), duree(Date.now() / 1000 - d.demarrage));
    ligne(t("mat_python"), d.python);
    id.append(dl);
    if (d.mode) id.append(el("p", "aide", t("mat_mode_note")));
  }
  // répartition de la mémoire : modèles chargés, système, libre
  const ram = el("section", "panneau");
  const charge = etat.charges.reduce((s, c) => s + c.taille_go, 0);
  const sys = Math.max(0, m.ram_utilisee_go - charge);
  const libre = m.ram_totale_go - m.ram_utilisee_go;
  ram.append(el("span", "libelle", t("mat_ram_titre")), barreStockage([["sys", sys], ["mod", charge]], m.ram_totale_go));
  const dl2 = el("dl", "lignes-mesure");
  dl2.style.marginTop = "10px";
  for (const c of etat.charges) dl2.append(el("dt", "mono", c.nom), el("dd", null, go(c.taille_go, 2)));
  dl2.append(el("dt", null, t("mat_ram_modeles")), el("dd", null, go(charge, 2)), el("dt", null, t("mat_ram_systeme")), el("dd", null, go(sys, 2)),
    el("dt", null, t("mat_ram_libre")), el("dd", null, go(libre, 2)));
  ram.append(dl2);
  if (etat.diagnostic?.verifications.some((v) => v.code === "bureau")) ram.append(el("p", "aide", T().diag.bureau()));
  haut.append(id, ram);
  contenu.push(haut);

  const grille = el("div", "grille-materiel");
  grille.style.marginTop = "16px";
  for (const mes of MESURES) {
    const v = m[mes.cle];
    const c = el("div", "mesure-carte");
    const val = el("p", v === null || v === undefined ? "valeur absente" : "valeur");
    if (v === null || v === undefined) val.textContent = t("non_dispo");
    else val.append(f(v, mes.d), el("small", null, mes.unite === "go" ? `/ ${go(m[mes.maxCle], 1)}` : mes.unite));
    const hist = etat.historique[mes.cle] || [];
    c.append(el("span", "libelle", t(mes.titre)), val, courbe(hist, mes.maxCle ? m[mes.maxCle] : mes.max));
    if (hist.length) c.append(el("span", "echelle", `min ${f(Math.min(...hist), mes.d)} · max ${f(Math.max(...hist), mes.d)} · 60 s`));
    grille.append(c);
  }
  contenu.push(grille);

  const journal = el("section", "panneau");
  journal.style.marginTop = "16px";
  journal.append(el("span", "libelle", t("mat_alertes_titre")));
  if (!etat.journal.length) journal.append(el("p", "aide", t("mat_alertes_vide")));
  else {
    const dl = el("dl", "lignes-mesure");
    dl.style.marginTop = "8px";
    for (const a of etat.journal.slice().reverse()) dl.append(el("dt", null, a.texte), el("dd", null, a.heure));
    journal.append(dl);
  }
  contenu.push(journal);
  zone.replaceChildren(...contenu);
}

// --- page Réglages ---------------------------------------------------------------------------

function ligneVerif(v) {
  const l = el("div", "verif");
  const fab = T().diag[v.code];
  l.append(el("span", `pastille ${v.niveau === "OK" ? "ok" : v.niveau === "ERREUR" ? "non" : "juste"}`, T().niveaux[v.niveau]),
    el("span", null, fab ? fab(v.params) : v.code));
  return l;
}

async function pageReglages() {
  const zone = $("page");
  zone.className = "page";
  const gauche = el("div", "reglages-grille");
  const p1 = el("section", "panneau");
  const langue = el("select");
  langue.append(new Option("Français", "fr"), new Option("English", "en"));
  langue.value = etat.langue;
  langue.addEventListener("change", () => changerLangue(langue.value));
  p1.append(el("h2", null, t("r_langue")), langue,
    el("h2", null, t("r_contexte")), selectContexte(etat.numCtx, null, (v) => { etat.numCtx = v; garder("edgefit.ctx", String(v)); }));
  const p2 = el("section", "panneau");
  const kv = selectKv(etat.typeKv, (v) => { etat.typeKv = v; garder("edgefit.kv", v); });
  p2.append(el("h2", null, t("r_kv")), el("p", "aide", t("r_kv_aide")), kv);
  for (const v of ["q8_0", "q4_0"]) {
    const d = pliKv(v);
    d.querySelector("summary").textContent = `${t("r_cmd")} : ${v}`;
    p2.append(d);
  }
  gauche.append(p1, p2);

  const droite = el("div", "reglages-grille");
  const sante = el("section", "panneau");
  const liste = el("div", "verifs");
  liste.append(el("div", "squelette"));
  const relancer = bouton(t("r_relancer"), "petit", async () => { liste.replaceChildren(el("div", "squelette")); remplir(await chargerDiagnostic(true)); });
  sante.append(el("h2", null, t("r_sante")), liste, relancer);
  const remplir = (diag) => {
    if (!diag) { liste.replaceChildren(el("p", "erreur-bloc", T().erreurs.reseau)); return; }
    liste.replaceChildren(...diag.verifications.map(ligneVerif));
    base.textContent = t("r_base", diag.faits.base);
    apropos.replaceChildren(...[[t("r_version"), "edgefit 0.1"], [t("mat_ollama"), diag.faits.ollama_version || "–"],
      [t("mat_carte"), diag.faits.carte || etat.metrique?.machine || "–"], [t("mat_python"), diag.faits.python]]
      .flatMap(([a, b]) => [el("dt", null, a), el("dd", null, b)]));
  };
  const donnees = el("section", "panneau");
  const base = el("p", "aide mono");
  const exp = el("a", "bouton", t("r_exporter"));
  exp.href = "/api/export";
  exp.download = "edgefit-export.json";
  const effConv = bouton(t("r_effacer_conv"), "danger", async () => {
    if (!(await confirmer(t("r_effacer_conv"), t("r_confirmer_conv"), t("effacer")))) return;
    await fetch("/api/conversations", { method: "DELETE" });
    chargerConversations();
  });
  const effBancs = bouton(t("r_effacer_bancs"), "danger", async () => {
    if (!(await confirmer(t("r_effacer_bancs"), t("r_confirmer_bancs"), t("effacer")))) return;
    await fetch("/api/bancs", { method: "DELETE" });
    chargerBancs();
  });
  const actions = el("div", "groupe-champs");
  actions.append(exp, effConv, effBancs);
  donnees.append(el("h2", null, t("r_donnees")), base, actions);
  const ap = el("section", "panneau");
  const apropos = el("dl", "lignes-mesure");
  const depot = el("a", "lien-discret", "github.com/yassine674/edgefit");
  depot.href = "https://github.com/yassine674/edgefit";
  depot.target = "_blank";
  depot.rel = "noopener";
  ap.append(el("h2", null, t("r_apropos")), apropos, depot);
  droite.append(sante, donnees, ap);

  const grille = el("div", "deux-colonnes");
  grille.append(gauche, droite);
  zone.replaceChildren(titrePage(t("r_titre")), grille);
  remplir(await chargerDiagnostic());
}

function changerLangue(l) {
  etat.langue = l;
  garder("edgefit.langue", l);
  appliquerLangue();
  afficher();
}
function appliquerLangue() {
  document.documentElement.lang = etat.langue;
  for (const e of document.querySelectorAll("[data-t]")) { const v = t(e.dataset.t); if (typeof v === "string") e.textContent = v; }
  $("bouton-langue").textContent = t("langue_autre");
  $("bouton-langue").title = t("langue_nom");
}
$("bouton-langue").addEventListener("click", () => changerLangue(etat.langue === "fr" ? "en" : "fr"));

// --- démarrage ------------------------------------------------------------------------------

(async function demarrer() {
  appliquerLangue();
  connecter();
  await Promise.all([chargerOllama(), chargerConversations(), chargerBancs()]);
  afficher();
})();
