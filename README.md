# bimdata-mcp

Serveur **MCP BIMData déployable** (HTTP, multi-utilisateurs) pour piloter depuis Claude une maquette IFC
hébergée sur [BIMData](https://bimdata.io) :

| Besoin | Outils |
|---|---|
| Sélectionner des éléments | `select_elements`, `combine_selections`, `show_selection`, `list_properties` |
| Export Excel de la sélection | `export_selection_excel` (attributs IFC + tous les Psets + toutes les Qto, une feuille par classe) |
| Fichier COBie | `generate_cobie` (maquette entière ou sélection) |
| Smart Views / BCF | `prepare_smart_view`, `prepare_bcf_topics` → `apply_plan` |
| Contrôle nommage / complétude + propositions | `check_naming_completeness`, `get_check_issues` |
| Cahier des charges de données → extraction Excel contrôlée | `requirements_template`, `import_data_requirements`, `run_requirements_check` |
| Corrections depuis un fichier corrigé | `prepare_corrections_from_file`, `prepare_corrections_from_check` → `apply_plan` |

Le serveur est **générique** : aucune règle, aucun référentiel ni aucun vocabulaire propre à un maître
d'ouvrage ou à un bailleur. Les exigences spécifiques d'un projet s'apportent par le cahier des charges importé
ou par des règles passées à `check_naming_completeness`.

Plus : `bimdata_browse`, `set_active_model`, `model_overview`, gestion des fichiers (`create_upload_link`,
`list_files`, `list_bimdata_documents`), des plans (`list_plans`, `show_plan`) et des cahiers des charges
(`show_requirements`, `edit_requirements`). 30 outils au total.

## Charte graphique et trame commune

Toutes les sorties suivent la charte BIMData.io et **la même trame** : Synthèse (logo, bloc d'identification fixe,
chiffres clés) → feuilles de données (en-têtes Primary, ligne de groupes jaune, zébrage, codes couleur d'état) →
Lisez-moi (sommaire + légende) ; même nommage de fichier, même mise en page d'impression, même structure de réponse
des outils. Détail : [`docs/CHARTE.md`](docs/CHARTE.md). Un test vérifie cette cohérence sur tous les livrables.

## Réutilisation de l'écosystème `code/MCP`

| Brique | Rôle ici |
|---|---|
| `bim-core` | contrats `ModelSnapshot`, `WritePlan` / `ActionResult` (pattern prepare → apply) |
| `bimdata-read` | client lecture, extraction + cache gzip du snapshot, diagnostic des routes |
| `bimdata-write` | écritures BCF / Smart Views / psets (étendu par sous-classe dans `bimdata.py`) |
| `bim-reporting` | tokens de la charte BIMData (couleurs, police) + garde-fou anti-injection de formules |
| `MCP_COBIe` | builder COBie 8 feuilles + exporteur (repris dans `cobie/`, car `mcp-cobie` épingle `bim-core<0.4`) |
| `audit-bim-mcp` | modèle de sécurité repris (plans, confirmation, journal, masquage d'erreurs) — sans ses profils clients |

Modules propres : vue « enregistrements » + syntaxe de champ unique (`model_view.py`), sélection
(`selection.py`), moteur de contrôle et propositions (`quality/engine.py`), import de cahier des charges
(`quality/spec_parser.py`), rapport Excel (`quality/report.py`), corrections (`corrections.py`), fichiers et
liens signés (`files.py`), auth (`auth.py`).

## Syntaxe des champs

Utilisée partout (critères, colonnes d'export, `color_by`, règles) :
`GlobalId`, `IfcClass`, `Name`, `LongName`, `Description`, `ObjectType`, `Tag`, `PredefinedType` (attributs),
`Building`, `Storey`, `Space`, `Zone`, `Classification`, `Material`, `Layer` (lecture seule),
`Pset_X.Prop` (propriété ou quantité d'un Pset), `*.Prop` (cherchée dans tous les Psets).

## Export Excel d'une sélection

Structuré comme le panneau **Propriétés** du viewer BIMData, une feuille par classe IFC (ou une seule avec
`layout="une_feuille"`), en-tête sur deux lignes : groupe puis propriété.

| Groupe | Colonnes |
|---|---|
| Attributs IFC (toujours) | UUID, Type, Nom, Description, Type d'objet, PredefinedType, Tag (+ Nom long et autres attributs présents) |
| Localisation | Bâtiment, Étage, Espace, Zone, Classification, Matériaux (`include_location`) |
| Psets | toutes les propriétés (`properties="all"`, défaut) ou une liste `Pset.Prop` / `Pset_X.*` |
| Qto | toutes les quantités `Qto_*` / `BaseQuantities` (`include_quantities`, défaut) |

Le classeur se réimporte pour corriger la maquette : seules les cellules modifiées sont retenues ; une colonne
ajoutée (Pset en ligne 1, propriété en ligne 2) crée la propriété.

## Écritures : toujours un plan, toujours une confirmation

`prepare_*` calcule et **persiste** un plan (cible figée, résumé, risques, détail). Rien n'est écrit tant que
`apply_plan(plan_id, confirm=True)` n'est pas appelé ; un plan visant une autre maquette que la maquette active
est refusé ; un plan ne s'applique qu'une fois. Chaque application est journalisée
(`data/users/<u>/journal/*.jsonl`). `MCP_ALLOW_WRITES=false` rend le serveur lecture seule.

Corrections de propriétés : `PATCH …/element/{uuid}/propertyset/{pset}/property/{id}` si la propriété existe
(l'API reconstruit un pset propre à l'élément, sans toucher les autres éléments qui partageaient le pset), sinon
ajout de la propriété au pset, sinon création du pset. Les champs spatiaux, matériaux, classifications et
classes IFC sont contrôlés mais **non corrigés** par l'API (à corriger dans le logiciel auteur).

## Cahier des charges de données

* **Gabarit** (`requirements_template`) : une ligne par exigence — classe IFC, Pset, propriété, obligatoire,
  type, valeurs autorisées, format (regex), longueur max, unicité (modèle / bâtiment / étage), phases, criticité.
* **Fichier client** : en-tête détecté (synonymes FR/EN, en-têtes sur deux lignes, cellules fusionnées),
  colonnes de phases cochées « x », sections « Documents » ignorées. Validé sur plusieurs cahiers des
  charges réels (tables d'attributs, annexes de spécification des données). En cas d'échec : `sheet`,
  `header_row`, `column_map`. L'interprétation se relit (`show_requirements`) et s'ajuste (`edit_requirements`).

Le rapport de contrôle contient : **Synthèse** (KPI, taux par exigence), **Erreurs à corriger** (une ligne par
anomalie, proposition + confiance, colonnes jaunes « Valeur corrigée » / « Appliquer » pré-remplies au-delà du
seuil de confiance), **Données contrôlées** (une ligne par élément, cellules vertes / rouges), **Exigences**.
Ce fichier, une fois complété, se réimporte tel quel. Un export de sélection modifié aussi : seules les cellules
différentes de la maquette produisent une correction ; une colonne « Pset.Propriété » ajoutée crée la propriété.

## Fichiers (serveur distant)

* Sorties : liens HTTPS signés et expirants `…/files/<u>/<id>/<nom>` (HMAC), option `publish_to_bimdata`
  pour déposer dans la GED du projet.
* Entrées : `create_upload_link` (page de dépôt navigateur → `file_id`), `bimdata_document_id` (GED),
  `file_url` (https publique, adresses privées refusées) ou `file_base64`.

## Lancer en local

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env            # MCP_AUTH_MODE=none + BIMDATA_API_KEY pour un essai local
bimdata-mcp --transport stdio   # Claude Desktop (command: bimdata-mcp, args: ["--transport","stdio"])
bimdata-mcp --port 8000         # HTTP : http://127.0.0.1:8000/mcp
```

## Déployer sur un serveur

1. Créer une application sur **BIMData Connect** (connect.bimdata.io → *Manage your applications*), client
   confidentiel, redirect URI `https://<domaine>/auth/callback` → `BIMDATA_OAUTH_CLIENT_ID` / `SECRET`.
2. `cp .env.example .env`, renseigner `MCP_DOMAIN`, `MCP_PUBLIC_URL`, `MCP_AUTH_MODE=oauth`,
   `MCP_JWT_SIGNING_KEY` et `MCP_FILE_SECRET` (`openssl rand -hex 32`).
3. DNS du domaine vers le serveur, ports 80/443 ouverts, puis `docker compose up -d --build`
   (Caddy obtient le certificat HTTPS). Vérifier `https://<domaine>/health`.

Le serveur refuse de démarrer en production sans HTTPS, sans auth ou sans secrets stables.
Données persistantes (sélections, contrôles, plans, fichiers, enregistrements OAuth chiffrés) : volume `/data`.

### Brancher Claude

* **Claude (web / desktop / mobile)** : Paramètres → Connecteurs → *Ajouter un connecteur personnalisé* →
  URL `https://<domaine>/mcp`. Chaque utilisateur se connecte avec son compte BIMData : il ne voit que ses
  projets, et les écritures portent son identité.
* **Claude Code** (mode `token`) :
  `claude mcp add --transport http bimdata https://<domaine>/mcp --header "Authorization: Bearer <jeton>"`
* **API Claude (connecteur MCP)** : `mcp_servers=[{"type":"url","url":"https://<domaine>/mcp","name":"bimdata","authorization_token":"<jeton>"}]`

## Tests

```bash
pytest -q                     # 14 tests : maquette synthétique + faux client BIMData, via le vrai client MCP
python scripts/smoke_real_snapshot.py <snapshot.json.gz> [cahier.xlsx] [phase]   # rejeu sur un cache réel
python scripts/generate_examples.py   # régénère docs/exemples/
```

## À valider en recette sur l'API réelle

Le développement a été testé sans accès réseau à BIMData (faux client fidèle aux routes de `bimdata-read`,
snapshot réel en cache). À vérifier sur un projet de test avant mise en production :

1. Écriture de propriétés : `PATCH …/propertyset/{id}/property/{id}` et création de propriété ; mise à jour des
   **attributs** (`Name`, `LongName`…) via le pset `attributes` de l'élément.
2. Dépôt GED : `POST /cloud/{c}/project/{p}/document` (multipart `file`, `name`, `parent_id`).
3. Application BIMData Connect : redirect URI, scopes nécessaires à l'API, durée des jetons.
4. Champs BCF `assigned_to` / `due_date` acceptés par `full-topic`.

## Pistes

* Remonter dans `bimdata-write` les routes ajoutées ici (PATCH propriété, GED, navigation).
* Corriger `bim_query.views` : l'arbre spatial BIMData type ses nœuds en *snake_case* (`storey`, `space`), d'où des
  étages / pièces toujours vides côté `bim-query` (contourné ici par `model_view.py`).
* Écriture des classifications (déjà supportée par `bimdata-write`) depuis le rapport.
* Plusieurs instances : partager `/data` (ou passer l'état sur Redis / PostgreSQL).
