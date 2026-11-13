// tableau de bord : reçoit les métriques par WebSocket et met à jour les cartes.
// Les valeurs sont insérées avec textContent (jamais innerHTML) : aucun texte reçu
// ne peut être interprété comme du code HTML.

const POINTS_HISTORIQUE = 60;

const CARTES = [
  { cle: "cpu_pct", titre: "CPU", unite: "%", max: 100 },
  { cle: "gpu_pct", titre: "GPU", unite: "%", max: 100 },
  { cle: "ram_utilisee_go", titre: "Mémoire", unite: "Go", maxCle: "ram_totale_go" },
  { cle: "temp_cpu_c", titre: "Température CPU", unite: "°C", max: 100 },
  { cle: "temp_gpu_c", titre: "Température GPU", unite: "°C", max: 100 },
  { cle: "puissance_w", titre: "Puissance", unite: "W" },
];

const SVG = "http://www.w3.org/2000/svg";
const elements = {};
const historiques = {};

function creerCartes() {
  const grille = document.getElementById("grille");
  for (const c of CARTES) {
    const carte = document.createElement("article");
    carte.className = "carte";
    const titre = document.createElement("h3");
    titre.textContent = c.titre;
    const valeur = document.createElement("p");
    valeur.className = "valeur";
    const courbe = document.createElementNS(SVG, "svg");
    courbe.setAttribute("class", "courbe");
    courbe.setAttribute("viewBox", `0 0 ${POINTS_HISTORIQUE - 1} 100`);
    courbe.setAttribute("preserveAspectRatio", "none");
    courbe.setAttribute("aria-hidden", "true");
    const ligne = document.createElementNS(SVG, "polyline");
    courbe.appendChild(ligne);
    carte.append(titre, valeur, courbe);
    grille.appendChild(carte);
    elements[c.cle] = { valeur, ligne };
    historiques[c.cle] = [];
  }
}

function afficherValeur(el, valeur, unite, decimales) {
  el.replaceChildren();
  if (valeur === null || valeur === undefined) {
    el.className = "valeur absente";
    el.textContent = "Non disponible";
    return;
  }
  el.className = "valeur";
  el.append(valeur.toFixed(decimales));
  const u = document.createElement("span");
  u.className = "unite";
  u.textContent = unite;
  el.appendChild(u);
}

function tracer(ligne, points, max) {
  const echelle = max || Math.max(...points, 1);
  const decalage = POINTS_HISTORIQUE - points.length;
  ligne.setAttribute(
    "points",
    points.map((v, i) => `${i + decalage},${100 - (v / echelle) * 100}`).join(" "),
  );
}

function mettreAJour(m) {
  document.getElementById("machine").textContent = m.machine;
  for (const c of CARTES) {
    const v = m[c.cle];
    const { valeur, ligne } = elements[c.cle];
    const decimales = c.unite === "Go" || c.unite === "W" ? 2 : 1;
    afficherValeur(valeur, v, c.maxCle ? `/ ${m[c.maxCle].toFixed(1)} Go` : c.unite, decimales);
    if (v === null || v === undefined) continue;
    const h = historiques[c.cle];
    h.push(v);
    if (h.length > POINTS_HISTORIQUE) h.shift();
    tracer(ligne, h, c.maxCle ? m[c.maxCle] : c.max);
  }
  const zone = document.getElementById("alertes");
  zone.replaceChildren(...m.alertes.map((texte) => {
    const p = document.createElement("p");
    p.textContent = texte;
    return p;
  }));
  zone.hidden = m.alertes.length === 0;
}

function statut(etat, texte) {
  const el = document.getElementById("statut");
  el.dataset.etat = etat;
  el.textContent = texte;
}

async function connecter(delai = 1000) {
  const protocole = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${protocole}://${location.host}/ws/metriques`);
  ws.onopen = () => { statut("connecte", "En direct"); delai = 1000; };
  ws.onmessage = (e) => mettreAJour(JSON.parse(e.data));
  ws.onclose = async () => {
    // le jeton change à chaque redémarrage du serveur : si l'API refuse l'accès,
    // inutile de réessayer, il faut rouvrir l'adresse affichée dans le terminal
    try {
      const r = await fetch("/api/metriques");
      if (r.status === 401) {
        statut("refuse", "Accès expiré");
        document.getElementById("machine").textContent =
          "Le serveur a redémarré : rouvrez l'adresse affichée dans le terminal.";
        return;
      }
    } catch { /* serveur injoignable : on réessaie */ }
    statut("coupe", "Reconnexion…");
    setTimeout(() => connecter(Math.min(delai * 2, 10000)), delai);
  };
}

// --- modèles et chat ----------------------------------------------------------
// Téléchargement et chat renvoient du NDJSON : une ligne JSON par événement,
// lue au fur et à mesure de son arrivée.

async function* lireNdjson(reponse) {
  const lecteur = reponse.body.getReader();
  const decodeur = new TextDecoder();
  let tampon = "";
  for (;;) {
    const { value, done } = await lecteur.read();
    if (done) break;
    tampon += decodeur.decode(value, { stream: true });
    let fin;
    while ((fin = tampon.indexOf("\n")) >= 0) {
      const ligne = tampon.slice(0, fin);
      tampon = tampon.slice(fin + 1);
      if (ligne.trim()) yield JSON.parse(ligne);
    }
  }
  if (tampon.trim()) yield JSON.parse(tampon);
}

async function erreurHttp(reponse) {
  // 422 : refusé par la validation du serveur (nom de modèle, message trop long...)
  if (reponse.status === 422) return "Demande invalide (vérifiez le nom du modèle).";
  try { return (await reponse.json()).erreur || `Erreur ${reponse.status}`; } catch { return `Erreur ${reponse.status}`; }
}

function envoyerJson(url, corps) {
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(corps) });
}

const go = (octets) => (octets / 1024 ** 3).toFixed(2);

let ollamaDisponible = false;

async function chargerModeles() {
  const etat = document.getElementById("etat-ollama");
  const liste = document.getElementById("liste-modeles");
  const choix = document.getElementById("choix-modele");
  const info = await (await fetch("/api/ollama")).json();
  ollamaDisponible = info.disponible;
  if (!info.disponible) {
    etat.className = "note erreur";
    etat.textContent = `Ollama ne répond pas sur ${info.url} : est-il installé et lancé ?`;
    // Ollama peut être lancé après l'ouverture de la page : on revérifie régulièrement
    setTimeout(chargerModeles, 10000);
    return;
  }
  etat.className = "note";
  etat.textContent = `Ollama ${info.version}`;
  const reponse = await fetch("/api/modeles");
  if (!reponse.ok) { etat.textContent = await erreurHttp(reponse); return; }
  const modeles = await reponse.json();
  const selection = choix.value;
  liste.replaceChildren(...modeles.map((m) => {
    const li = document.createElement("li");
    const nom = document.createElement("span");
    nom.className = "nom";
    nom.textContent = m.nom;
    const details = document.createElement("span");
    details.className = "details";
    details.textContent = [m.parametres, m.quantification, `${m.taille_go.toFixed(2)} Go`].filter(Boolean).join(" · ");
    li.append(nom, details);
    return li;
  }));
  if (modeles.length === 0) {
    const li = document.createElement("li");
    li.textContent = "Aucun modèle installé : téléchargez-en un ci-dessous.";
    liste.appendChild(li);
  }
  choix.replaceChildren(...modeles.map((m) => new Option(m.nom, m.nom)));
  if (modeles.some((m) => m.nom === selection)) choix.value = selection;
  document.getElementById("modeles-installes").replaceChildren(...modeles.map((m) => new Option(m.nom)));
  for (const [i, li] of [...liste.children].entries()) {
    if (!modeles[i]) continue;
    const bouton = document.createElement("button");
    bouton.type = "button";
    bouton.className = "lien";
    bouton.textContent = "Vérifier";
    bouton.addEventListener("click", () => verifierModele(modeles[i].nom));
    li.appendChild(bouton);
  }
}

document.getElementById("form-telecharger").addEventListener("submit", async (e) => {
  e.preventDefault();
  const champ = document.getElementById("nom-telecharger");
  const bouton = document.getElementById("bouton-telecharger");
  const zone = document.getElementById("progression");
  const barre = document.getElementById("barre");
  const texte = document.getElementById("texte-progression");
  bouton.disabled = true;
  zone.hidden = false;
  barre.hidden = false;
  texte.className = "note";
  texte.textContent = "Démarrage…";
  barre.removeAttribute("value");  // barre indéterminée tant que la taille est inconnue
  try {
    const reponse = await envoyerJson("/api/modeles/telecharger", { nom: champ.value.trim() });
    if (!reponse.ok) throw new Error(await erreurHttp(reponse));
    for await (const evt of lireNdjson(reponse)) {
      if (evt.type === "progression") {
        if (evt.total) {
          barre.value = evt.fait / evt.total;
          texte.textContent = `${evt.statut} — ${go(evt.fait)} / ${go(evt.total)} Go`;
        } else {
          texte.textContent = evt.statut;
        }
      } else if (evt.type === "erreur") {
        throw new Error(evt.erreur);
      } else if (evt.type === "fin") {
        barre.value = 1;
        texte.textContent = "Téléchargement terminé.";
        champ.value = "";
        await chargerModeles();
      }
    }
  } catch (err) {
    barre.hidden = true;  // sinon la barre « en cours » continuerait de s'animer à côté de l'erreur
    texte.className = "note erreur";
    texte.textContent = err.message;
  } finally {
    bouton.disabled = false;
  }
});

const historiqueChat = [];

function ajouterBulle(role, texte) {
  document.getElementById("conversation-vide")?.remove();
  const bulle = document.createElement("div");
  bulle.className = `message ${role}`;
  bulle.textContent = texte;
  document.getElementById("conversation").appendChild(bulle);
  return bulle;
}

function defiler() {
  const conv = document.getElementById("conversation");
  conv.scrollTop = conv.scrollHeight;
}

function resumeMesures(m) {
  const f = (v, d = 2) => (v === null || v === undefined ? "—" : v.toFixed(d));
  const morceaux = [
    `TTFT ${f(m.ttft_mesure_s)} s`,
    `${f(m.debit_tokens_s, 1)} tok/s`,
    `prefill ${f(m.debit_prefill_tokens_s, 0)} tok/s (${m.tokens_prompt} tokens)`,
  ];
  // au-delà de quelques centièmes de seconde, le modèle n'était pas encore en mémoire :
  // ce temps est compris dans le TTFT, on l'affiche pour ne pas le confondre avec le calcul
  if (m.chargement_s > 0.05) morceaux.push(`dont chargement ${f(m.chargement_s)} s`);
  if (m.memoire_chargee_go) morceaux.push(`mémoire ${f(m.memoire_chargee_go)} Go`);
  return morceaux.join(" · ");
}

document.getElementById("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    document.getElementById("form-chat").requestSubmit();
  }
});

document.getElementById("form-chat").addEventListener("submit", async (e) => {
  e.preventDefault();
  const champ = document.getElementById("question");
  const bouton = document.getElementById("envoyer");
  const modele = document.getElementById("choix-modele").value;
  const question = champ.value.trim();
  if (!question) return;
  if (!modele) {
    ajouterBulle("assistant erreur", ollamaDisponible
      ? "Aucun modèle installé : téléchargez-en un d'abord."
      : "Ollama ne répond pas : le chat sera disponible dès qu'il sera lancé.");
    return;
  }

  champ.value = "";
  bouton.disabled = true;
  ajouterBulle("user", question);
  historiqueChat.push({ role: "user", content: question });
  const bulle = ajouterBulle("assistant", "…");
  let reponseTexte = "";
  try {
    const num_ctx = Number(document.getElementById("choix-contexte").value);
    const reponse = await envoyerJson("/api/chat", { modele, num_ctx, messages: historiqueChat });
    if (!reponse.ok) throw new Error(await erreurHttp(reponse));
    for await (const evt of lireNdjson(reponse)) {
      if (evt.type === "token") {
        reponseTexte += evt.texte;
        bulle.textContent = reponseTexte;
      } else if (evt.type === "erreur") {
        throw new Error(evt.erreur);
      } else if (evt.type === "fin") {
        const mesures = document.createElement("p");
        mesures.className = "mesures";
        mesures.textContent = resumeMesures(evt.metriques);
        mesures.title = "TTFT : délai avant le premier mot, mesuré par edgefit (chargement du modèle compris). "
          + "tok/s : débit de génération mesuré par Ollama. Prefill : lecture du message par le modèle. "
          + "Mémoire : occupée réellement par le modèle chargé (selon Ollama), à comparer à l'estimation.";
        bulle.after(mesures);
      }
      defiler();
    }
    historiqueChat.push({ role: "assistant", content: reponseTexte });
  } catch (err) {
    // la question sans réponse est retirée de l'historique envoyé au modèle
    historiqueChat.pop();
    bulle.className = "message assistant erreur";
    bulle.textContent = err.message;
  } finally {
    bouton.disabled = false;
    champ.focus();
    defiler();
  }
});

// --- compatibilité ------------------------------------------------------------

const CONTEXTES = [2048, 4096, 8192, 16384, 32768, 65536, 131072];
const fmt = (v, d = 1) => (v === null || v === undefined ? "—" : v.toFixed(d));
const contexteLisible = (n) => (n >= 1024 ? `${n / 1024}K` : String(n));

function remplirContextes(select, valeur) {
  select.replaceChildren(...CONTEXTES.map((n) => new Option(`${contexteLisible(n)} tokens`, n)));
  select.value = valeur;
}

function el(balise, classe, texte) {
  const e = document.createElement(balise);
  if (classe) e.className = classe;
  if (texte !== undefined) e.textContent = texte;
  return e;
}

function bouton(texte, action) {
  const b = el("button", "secondaire", texte);
  b.type = "button";
  b.addEventListener("click", action);
  return b;
}

function utiliserDansLeChat(nom, numCtx) {
  const choix = document.getElementById("choix-modele");
  if ([...choix.options].some((o) => o.value === nom)) choix.value = nom;
  const ctx = document.getElementById("choix-contexte");
  if (![...ctx.options].some((o) => Number(o.value) === numCtx)) ctx.add(new Option(`${contexteLisible(numCtx)} tokens`, numCtx));
  ctx.value = numCtx;
  document.getElementById("question").focus();
}

function preparerTelechargement(nom) {
  const champ = document.getElementById("nom-telecharger");
  champ.value = nom;
  champ.scrollIntoView({ behavior: "smooth", block: "center" });
  champ.focus();
}

function commande(texte) {
  const bloc = el("pre", "commande");
  bloc.textContent = texte;
  return bloc;
}

function barreMemoire(d) {
  // poids + cache KV + buffers, sur une échelle qui contient aussi la limite utilisable
  const e = d.estimation_go, limite = d.budget_go.utilisable;
  const echelle = Math.max(e.total, limite) || 1;
  const barre = el("div", "barre-memoire");
  barre.setAttribute("role", "img");
  barre.setAttribute("aria-label", `Estimation ${fmt(e.total, 2)} Go pour ${fmt(limite, 2)} Go utilisables`);
  for (const [cle, nom] of [["poids", "Poids"], ["cache_kv", "Cache KV"], ["buffers", "Buffers"]]) {
    const seg = el("span", `segment ${cle}`);
    seg.style.width = `${(e[cle] / echelle) * 100}%`;
    seg.title = `${nom} : ${fmt(e[cle], 2)} Go`;
    barre.appendChild(seg);
  }
  const repere = el("span", "limite");
  repere.style.left = `${(limite / echelle) * 100}%`;
  repere.title = `Utilisable : ${fmt(limite, 2)} Go`;
  barre.appendChild(repere);
  const legende = el("p", "legende");
  for (const [cle, nom] of [["poids", "Poids"], ["cache_kv", "Cache KV"], ["buffers", "Buffers"]]) {
    const item = el("span", "item");
    item.append(el("span", `pastille ${cle}`), `${nom} ${fmt(e[cle], 2)} Go`);
    legende.appendChild(item);
  }
  const item = el("span", "item");
  item.append(el("span", "pastille limite"), `Utilisable ${fmt(limite, 2)} Go`);
  legende.appendChild(item);
  return [barre, legende];
}

const VERDICTS = {
  ok: ["ok", "Rentre en mémoire"],
  ok_apres_dechargement: ["alerte", "Rentre, après déchargement des modèles déjà chargés par Ollama"],
  trop_gros: ["erreur", "Ne rentre pas"],
};

function afficherPiles(d) {
  const zone = el("div", "piles");
  for (const p of d.piles) {
    const carte = el("article", "pile");
    carte.append(el("h4", null, p.titre));
    carte.append(el("p", "pile-reglage", `${p.format} · ${contexteLisible(p.num_ctx)} tokens · cache ${p.type_kv}`));
    const details = [`${fmt(p.total_go, 2)} Go`, `reste ${fmt(p.reste_go, 2)} Go`];
    if (p.debit_max_tokens_s) details.push(`plafond ~${fmt(p.debit_max_tokens_s, 0)} tok/s`);
    carte.append(el("p", "note", details.join(" · ")));
    const etapes = el("p", "badges");
    if (p.telechargement) etapes.append(el("span", "badge", p.nom ? "autre fichier" : "autre fichier, tag à trouver"));
    if (p.reglage_serveur) etapes.append(el("span", "badge", "réglage serveur"));
    if (p.qualite_degradee) etapes.append(el("span", "badge erreur", "qualité dégradée"));
    if (!p.telechargement && !p.reglage_serveur) etapes.append(el("span", "badge ok", "prêt"));
    carte.append(etapes);
    if (!p.telechargement && !p.reglage_serveur && d.source === "installé") {
      carte.append(bouton("Utiliser dans le chat", () => utiliserDansLeChat(d.modele, p.num_ctx)));
    } else if (p.telechargement && p.nom) {
      carte.append(bouton("Préparer le téléchargement", () => preparerTelechargement(p.nom)));
    } else if (!p.telechargement && d.source === "registre") {
      carte.append(bouton("Préparer le téléchargement", () => preparerTelechargement(d.modele)));
    }
    if (p.reglage_serveur) {
      const plus = el("details");
      plus.append(el("summary", null, "Commande du réglage serveur"));
      plus.append(commande(`sudo systemctl edit ollama\n[Service]\nEnvironment="OLLAMA_FLASH_ATTENTION=1"\n`
        + `Environment="OLLAMA_KV_CACHE_TYPE=${p.type_kv}"\n# puis : sudo systemctl restart ollama`));
      carte.append(plus);
    }
    zone.appendChild(carte);
  }
  return zone;
}

function afficherSolutions(solutions) {
  const zone = el("div", "solutions");
  for (const cat of solutions.categories) {
    const bloc = el("details", "categorie");
    bloc.open = true;
    bloc.append(el("summary", null, cat.categorie));
    const liste = el("ul");
    for (const p of cat.pistes) {
      const li = el("li");
      const tete = el("div", "piste-tete");
      tete.append(el("strong", null, p.titre));
      if (p.gain_go !== undefined) {
        tete.append(el("span", p.rentre ? "badge ok" : "badge", `−${fmt(p.gain_go, 2)} Go${p.rentre ? " · suffit" : " · ne suffit pas seul"}`));
      }
      li.append(tete, el("p", "note", p.detail));
      if (p.commande) li.append(commande(p.commande));
      if (p.action?.type === "contexte") {
        li.append(bouton(`Vérifier avec ${contexteLisible(p.action.valeur)} tokens`, () => {
          document.getElementById("compat-contexte").value = p.action.valeur;
          verifierModele(document.getElementById("compat-modele").value);
        }));
      } else if (p.action?.type === "telecharger") {
        li.append(bouton("Vérifier cette variante", () => verifierModele(p.action.valeur)));
      } else if (p.action?.type === "verifier") {
        li.append(bouton("Vérifier ce modèle", () => verifierModele(p.action.valeur)));
      }
      liste.appendChild(li);
    }
    bloc.appendChild(liste);
    zone.appendChild(bloc);
  }
  return zone;
}

function afficherCompat(d) {
  const zone = document.getElementById("resultat-compat");
  const [classe, texte] = VERDICTS[d.verdict];
  const verdict = el("p", `verdict ${classe}`, texte);
  if (d.verdict === "trop_gros") verdict.append(el("span", "manque", ` : il manque ${fmt(d.solutions.manque_go, 2)} Go`));

  const b = d.budget_go;
  const infos = [
    `${d.source === "installé" ? "Installé" : "À télécharger"} : ${fmt(d.taille_fichier_go, 2)} Go${d.format ? ` (${d.format})` : ""}`,
    `contexte max ${d.contexte_max ? contexteLisible(d.contexte_max) : "inconnu"}`,
  ];
  if (d.couches_glissantes) infos.push(`${d.couches_glissantes}/${d.architecture.num_layers} couches à fenêtre glissante`);
  const memoire = `Mémoire libre ${fmt(b.disponible, 2)} Go`
    + (b.liberable ? ` + ${fmt(b.liberable, 2)} Go libérables par Ollama` : "")
    + ` − marge ${fmt(b.marge, 2)} Go = ${fmt(b.utilisable, 2)} Go utilisables (sur ${fmt(b.totale, 1)} Go)`;

  const contenu = [verdict, ...barreMemoire(d), el("p", "note", infos.join(" · ")), el("p", "note", memoire)];
  if (d.piles.length) {
    contenu.push(el("h3", "sous-titre", d.verdict === "trop_gros" ? "Réglages qui rentrent" : "Réglages possibles"));
    contenu.push(afficherPiles(d));
  } else if (d.verdict === "trop_gros") {
    contenu.push(el("p", "note erreur", "Aucune combinaison (quantification, contexte, cache KV) ne rentre sur cette carte pour l'instant."));
  }
  if (d.solutions) {
    contenu.push(el("h3", "sous-titre", "Pistes, de la plus simple à la plus lourde"));
    contenu.push(afficherSolutions(d.solutions));
  }
  zone.replaceChildren(...contenu);
}

async function verifierModele(nom) {
  const champ = document.getElementById("compat-modele");
  const bouton = document.getElementById("bouton-compat");
  const zone = document.getElementById("resultat-compat");
  champ.value = nom.trim();
  if (!champ.value) return;
  bouton.disabled = true;
  zone.replaceChildren(el("p", "note", "Lecture des informations du modèle…"));
  try {
    const reponse = await envoyerJson("/api/compatibilite", {
      modele: champ.value,
      num_ctx: Number(document.getElementById("compat-contexte").value),
      type_kv: document.getElementById("compat-kv").value,
    });
    if (!reponse.ok) {
      // 422 avec un texte : refus expliqué par le serveur (modèle introuvable, protégé...) ;
      // 422 avec une liste : refus de la validation (nom de modèle mal formé)
      let corps = {};
      try { corps = await reponse.json(); } catch { /* pas de JSON */ }
      throw new Error(typeof corps.detail === "string" ? corps.detail
        : corps.erreur || (reponse.status === 422 ? "Nom de modèle invalide." : `Erreur ${reponse.status}`));
    }
    afficherCompat(await reponse.json());
  } catch (err) {
    zone.replaceChildren(el("p", "note erreur", err.message));
  } finally {
    bouton.disabled = false;
  }
}

document.getElementById("form-compat").addEventListener("submit", (e) => {
  e.preventDefault();
  verifierModele(document.getElementById("compat-modele").value);
});

remplirContextes(document.getElementById("compat-contexte"), 4096);
remplirContextes(document.getElementById("choix-contexte"), 4096);

creerCartes();
connecter();
chargerModeles();
