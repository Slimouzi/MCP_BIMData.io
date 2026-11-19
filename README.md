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
| Audit des classifications | `audit_classifications` : manquantes, incohérences (classe IFC ↔ famille UniFormat, type, format, codes multiples) + propositions, rapport Excel |
| Classifications | `prepare_classification_update` → `apply_plan` (ou colonne « Classification » d'un export modifié) |
| Menu d'audit (10 audits par famille : général, nommage, conflits, réglementaire, cahier des charges, avancés) | `list_audits` (prérequis + message en français) → choix d'**un** audit → `run_audit(audit)` (hypothèses + jeton) → validation → `run_audit(audit, hypotheses_validees=jeton)` : rapport PDF (annexe Excel), puis proposition BCF ou Smart View |
| Structuration et intégrité IFC | `run_structure_check` : règles S1–S11 (site / bâtiment, contenance, étages, élément posé sur son étage, réservations, porte au même étage que son mur, sens d'ouverture, GlobalId, géoréférencement, sans géométrie, local hors de l'emprise du bâtiment) |
| Conflits (clash) | `run_clash_check(set_a, set_b)` (règle General Intersection : doublon, inclus, chevauchement, tolérances, exceptions, sévérité, 6 regroupements, feuille Volumes) ; `run_clash_matrix` (matrice par défaut ou Excel de `clash_matrix_template`) |
| Comparaison avec Solibri | `compare_clash_results(check_id, file_id)` : export Solibri (BCF, Excel ou CSV avec les GlobalId des deux éléments) comparé paire par paire — communs, MCP seulement, Solibri seulement, écarts contre l'objectif (5 %) |
| Géométrie et modélisation | `run_geometry_quality_check` : G1 locaux (surface, hauteur, gaines en information), G2 locaux qui se chevauchent, G3 maillage des locaux, G4 doublons, G5 murs non raccordés, G6 murs sans appui, G7 escaliers (marche, giron, 2h + g, échappée), G8 garde-corps |
| Distances entre composants | `run_distance_check(set_a, set_b, mode, distance_m, direction, same_space_only)` (règle 222, distance mesurée sur les maillages) |
| Espaces libres et dégagements | `run_clearance_check(preset='portes' \| targets…, front_m, back_m, side_m, height_m)` (règles 226 / 248 : zone dans le repère de la cible, empiètement mesuré) |
| Accessibilité PMR | `run_accessibility_check(referentiel, scope)` : A1 portes, A2 manœuvre (à paramétrer), A3 cercle de 1,50 m, A4 usage WC / lavabo (à paramétrer), A5 marches, A6 HandicapAccessible |
| Sécurité incendie | `run_fire_check(exits, f3_referentiel, …)` : F1 FireRating, F2 parois entre compartiments, F3 distance à la sortie suivant l'axe des circulations (par référentiel), F4 unités de passage, F5 culs-de-sac, F6 espaces d'attente sécurisés |
| Index géométrique | `build_geometry_index` (automatique au premier contrôle), `geometry_info` (AABB, OBB, volume, axes) |
| Retrait de classification | `prepare_classification_removal(system, notation?)` → `apply_plan` ; dans un fichier : code supprimé de la cellule, ou `<vide>` pour tout retirer |

Le serveur est **générique** : aucune règle, aucun référentiel ni aucun vocabulaire propre à un maître
d'ouvrage ou à un bailleur. Les exigences spécifiques d'un projet s'apportent par le cahier des charges importé
ou par des règles passées à `check_naming_completeness`.

Plus : `bimdata_browse`, `set_active_model`, `model_overview`, gestion des fichiers (`create_upload_link`,
`list_files`, `list_bimdata_documents`), des plans (`list_plans`, `show_plan`) et des cahiers des charges
(`show_requirements`, `edit_requirements`). 32 outils au total.

## Couche géométrique

Le fichier IFC source de la maquette (GED BIMData, URL signée relue à chaque téléchargement, gzip détecté) est lu
avec IfcOpenShell une fois par version de fichier. Pour chaque élément physique et chaque local : AABB, OBB à axe
vertical, volume, maillage fermé oui/non, axes locaux — en mètres, repère projet. Cache :
`MCP_DATA_DIR/cache/geometry/<maquette>_<sha1>/` (`elements.sqlite` avec R*Tree, `meshes.npz`, `ifc.json` pour les
relations IFC). Mesuré sur une maquette de 28 Mo / 2 000 éléments : 9 s de construction, 15 ms de relecture.
`MCP_MAX_IFC_MB` (800) plafonne le téléchargement ; `MCP_GEOMETRY_THREADS` le nombre de processus de triangulation.
Calcul de l'index dans un processus séparé (`python -m bimdata_mcp.geometry.build_worker`) : un plantage natif de la
triangulation n'emporte pas le serveur et la mémoire est rendue à la fin (`MCP_GEOMETRY_SUBPROCESS=0` : calcul en
direct). Triangulation : `MCP_GEOMETRY_LIBRARY`, défaut `hybrid-cgal-simple-opencascade`, 4 à 8 fois plus rapide et
7 fois moins gourmande en mémoire qu'`opencascade` ; elle plantait par intermittence avec IfcOpenShell 0.8.3, plus avec
0.8.5 (60 calculs sur 60) — d'où la dépendance `ifcopenshell>=0.8.5,<0.9` (0.9 a renommé les noyaux : migration à
part). En cas de plantage, le calcul reprend automatiquement avec OpenCascade (noté dans l'état de l'index).
Comparaison des deux bibliothèques sur trois maquettes, tous contrôles (faute d'export Solibri) : boîtes à 5 mm près
(sauf 5 B-rep ouverts sur 9 948), règles à 0–0,4 % près, conflits à 0–3,7 % près (maquette test : 506 contre 513) —
écarts tous sur des contacts à plat ou des maillages ouverts, instables avec l'une comme l'autre. Option du clash
`contact_m` (0,001 conseillé, désactivée par défaut) : surfaces à moins de 1 mm = contact, pas conflit — stabilise ces
cas mais change sensiblement le résultat (maquette test, Architecture × Structure : +127 / −49), à valider.
Budget mémoire : `MCP_GEOMETRY_MAX_MEMORY_MB` (défaut 75 % de la mémoire physique). Pic estimé avant le calcul
(≈ 12 × la taille du fichier + 0,4 Go + 20 Mo par fil en hybride ; + 0,6 Go + 0,1 Go par fil en OpenCascade, budget recalculé en cas de repli) : le nombre de fils est réduit pour tenir, le calcul est refusé
avec un message clair si un seul fil ne tient pas, et le processus de calcul est arrêté proprement s'il dépasse la limite.
GlobalId en double : chaque occurrence est indexée « GlobalId#idSTEP » (pas d'écrasement), signalée par S8 (bloquant) ;
BCF et Smart Views reçoivent le GlobalId.
Agrégats sans géométrie propre (murs en couches `IfcBuildingElementPart`, escaliers en volées) : géométrie réunie de
leurs parties (`from_parts`), utilisée par les règles « élément » (S4, G5, G6, dégagements, F2) ; le clash et les
doublons testent les parties.
Exceptions du clash (actives par défaut, réglables par `exceptions`) : traversée de mur ou de dalle à 89–90°, luminaire
en faux plafond, bouche affleurante, isolant de réseau (IfcCovering INSULATION autour de son propre réseau : même axe,
le centre de l'un dans la boîte de l'autre ; vaut aussi pour les doublons et inclusions — 8 faux bloquants retirés sur
la maquette test).
Au-delà de `MCP_GEOMETRY_BACKGROUND_MB` (100 Mo ≈ 30 s au débit mesuré de 3 Mo/s ; 0 = toujours en direct), l'index
se calcule en tâche de fond : les outils répondent « en cours, relancer dans ≈ N s » jusqu'à ce qu'il soit prêt.
G6 : mur au niveau de l'étage avec une dalle au plus 0,35 m dessous = « chape non modélisée » (information) ; tout
autre écart de plus de 2 cm = mur flottant (anomalie).

Une AABB seule ne prouve jamais un conflit : elle sert à trouver les candidats, le maillage conclut. Phase fine en
numpy sur les maillages du cache (pas de relecture de l'IFC) : la région d'intersection est délimitée par les sommets
de chaque élément contenus dans l'autre et par les croisements arête × face ; sa boîte orientée donne les pénétrations
horizontale et verticale comparées à `tol_h_m` / `tol_v_m`. Volume d'intersection : enveloppe convexe de cette région
(exact pour une intersection convexe), échantillonnage quand `tol_volume_m3` est demandée ; `None` si un maillage est
ouvert. Disciplines : `discipline_property` si fournie, sinon Fluides (classes réseaux), Structure (poutres, poteaux,
fondations, murs et dalles LoadBearing), Architecture (le reste).
Les anomalies de ces contrôles portent en plus `rule_id`, `elements` (rôles), `measure`, `threshold`, `where`
(champ « location » de la spécification, renommé pour ne pas écraser l'emplacement de champ existant), `bbox`,
`method`.

### Seuils PMR et incendie

Valeurs de départ de la spécification (BHC 2015, ERP 2017, 1 UP = 0,90 m, 2 UP = 1,40 m), **indicatives et à faire
valider** : toutes paramétrables, rappelées dans chaque rapport avec leur source. A2 et A4 ne tournent que si leurs
dimensions sont fournies. Contrôles d'aide à la décision, pas d'attestation de conformité ; détail de F3, F5, F6
ci-dessous. Côté d'ouverture d'une porte : axe +Y de son placement (convention IFC,
`OperationType` ne donne que le côté des gonds), contrôlé seul pour une porte battante, les deux côtés sinon. Sols
minces (≤ 10 cm) au seuil : jamais des obstacles ; la marche au seuil est la règle A7 « ressaut » (2 cm, information).
A3 et A6 : locaux désignés par le projet (`scope`) uniquement. Sorties (`exits='auto'`) : portes extérieures des niveaux
d'accès (`access_storeys`, plusieurs pour un bâtiment en escalier ; défaut : étages dont le nom, ou une partie d'un nom
combiné « … & … », commence par RDC, Rez ou Ground ; listés dans le rapport pour confirmation) + portes qui donnent sur un local contenant un escalier (géométrie ; le nom STAIR / ESCALIER n'est qu'un indice) ;
`stair_door_distance_m` ajoute, sur demande, les portes proches d'un escalier situé hors de tout local. Une porte de
balcon n'est jamais une sortie, ni une trappe de gaine technique (GT, LT…) qui ouvre sur une cage ; un étage sans sortie
détectée est déclaré « non contrôlé ». Escalier ouvert (local contenant un escalier, sans porte) : listé dans le rapport,
compté comme sortie avec `open_stairs=True` (on est sorti dès qu'on y entre).

Évacuation (F3, F5, F6) — aide à la décision : la conformité se juge avec le bureau de contrôle et la commission de
sécurité selon le type et la catégorie de l'établissement.
- Référentiel (`f3_referentiel`) : `ERP` (niveau d'accès, CO 43 : vers une sortie sur l'extérieur ou un dégagement
  protégé, 50 m si le choix existe entre plusieurs sorties, 30 m sinon ; étages et sous-sols, CO 49 : vers un escalier
  protégé, 40 m, 30 m en cul-de-sac, 30 m vers un escalier non protégé), `ERP_J` (40 / 30 m), `habitation_3A` (10 m
  de la porte palière à l'escalier), `habitation_3B_4` (25 m, circulations à l'air libre), `code_travail` (40 m en
  étage ; 10 m en cul-de-sac), `personnalise` (`max_distance_m`). Sans référentiel ni distance : F3 non contrôlée.
- Départ (`f3_origin`) : point le plus éloigné du local (ERP, code du travail) ou porte palière du logement
  (habitation ; `landing_doors`, sinon détectées par le nom « palière / Ppal » ou entre logement et circulation commune).
- Escaliers protégés (`protected_stairs` : `all`, `none` ou sélection, circulations horizontales protégées comprises) ;
  inconnus en ERP : entre 30 et 40 m, résultat « non concluant ». Un escalier ouvert n'est jamais protégé.
- Mesure suivant l'axe des circulations (CO 49) : grille de 0,10 m (`path_cell_m`) par étage et par niveau de plancher ;
  murs, poteaux et fenêtres coupés à 1 m du plancher comme obstacles ; portes prolongées de 0,30 m pour traverser leur
  mur ; passages libres franchissables ; le chemin est pénalisé à moins de 0,75 m d'un mur, donc tenu dans l'axe des
  couloirs ; longueur = ligne droite du point le plus éloigné à la porte du local de départ, puis axe des circulations
  sans les crans de la grille. Chemin tendu au ras des murs (`tendu_m`), grille brute, vol d'oiseau et orthogonale en
  complément ; polyligne jointe (`measure.chemin`). Un calcul par cage d'escalier et par sortie extérieure.
- F5 cul-de-sac : longueur parcourue avant d'avoir le choix entre deux sorties distinctes (les plus courts chemins
  vers les deux sorties les plus proches se séparent) ; un niveau à une seule issue est tout entier en cul-de-sac.
  Limite `dead_end_max_m` (ERP : 10 m, ERP de 5e catégorie — à confirmer pour les autres) ; F3 applique le seuil
  « cul-de-sac » aux locaux concernés.
- F6 espaces d'attente sécurisés (`eas`) : chaque local doit en atteindre un (ou une sortie extérieure au niveau
  d'accès) sans marche de plus de 2 cm ni porte de moins de `pmr_door_min_m` (0,90 m, comme A1), escaliers
  infranchissables, dans les distances de F3 ; un local qui n'y arrive que par un escalier est en anomalie. Capacité
  avec `effectif` (nombre ou {étage: nombre}) : 2 places de 0,80 × 1,30 m jusqu'à 50 personnes, + 1 par tranche de 50.
- Mobilier ignoré ; locaux sans chemin vers une sortie en information, avec leur diagnostic ; volumes de zone (surfaces
  IPMS, SHAB, « Surface ») écartés.

Lisibilité des rapports (audit du 18/11/2026) : une ligne par cause, pas par paire ni par obstacle.
- A6 sans périmètre désigné : une seule ligne si HandicapAccessible manque partout ; contrôle élément par élément sur
  `scope` seulement.
- Dégagements : une ligne par porte et par côté, avec la liste des obstacles ; tolérance par défaut 5 cm.
- S7 : une ligne par type de porte, avec le nombre et la liste des portes.
- G4 et doublons du clash : une ligne par groupe superposé (pile de compteurs, triplet de tuyaux) ; les paires restent
  dans la mesure pour `compare_clash_results`. Le taux de conformité compte les éléments regroupés.
- G2 : chevauchements d'un volume de zone (surfaces IPMS, SHAB…, même détecteur que F3) en information.
- G6 : « chape non modélisée » en information aussi pour un mur posé au niveau fini (jusqu'à 0,35 m au-dessus du niveau)
  ou sur la structure de l'étage du dessous sans dalle au droit du mur.
- Clash : un tronçon de réseau entièrement inclus dans un mur ou une dalle, perpendiculaire et de la longueur de son
  épaisseur (à 2 cm près), est une traversée (exception), pas une inclusion.
- Sorties : les portes de moins de 0,60 m ne sont jamais des sorties ; les escaliers ouverts dont la cage rejoint le
  niveau d'accès comptent par défaut comme dégagements non protégés (`open_stairs`).
- G7 / A5 : un sous-escalier ou une volée prend les valeurs de son escalier parent ; sans aucune cote, hauteur et
  giron estimés sur le maillage (G7 : information « à vérifier » ; A5 : non contrôlé).
- Noms : les rapports affichent les noms actuels de BIMData (le nom du fichier IFC reste dans l'index) ; une note
  signale les éléments renommés depuis l'export IFC (le simple suffixe d'identifiant Revit « :181719 » ne compte pas).
- Publication dans la GED BIMData : après chaque audit, `proposition_ged` propose de déposer le rapport PDF et
  l'Excel dans le dossier par défaut « Audits BIM/<maquette> » ou un autre au choix (`list_ged_folders` liste les
  dossiers existants). `prepare_ged_publication(check_id | file_ids | file_paths, folder)` prépare le plan : dossiers
  manquants créés (chemin « A/B/C »), documents déjà présents dans le dossier non redéposés ; `apply_plan` après
  accord (reprenable). Tout document peut être publié : fichier produit par le serveur ou fichier local (serveur sur
  le poste seulement).
- Encart « L'essentiel » en tête de chaque rapport PDF (lecture critique avant les chiffres) : indicateurs
  automatiques de fiabilité par règle (`quality/insight.py` : taux d'anomalies anormal, écarts de 5 cm au plus
  majoritaires, obstacle dominant, objets probablement hors champ comme éviers ou gaines, données d'entrée manquantes)
  et recommandation sur les BCF ; l'analyse d'expert rédigée par l'assistant (fiabilité par règle, à retenir,
  prérequis et responsables, recommandation) les remplace via `annotate_audit_report(check_id, analyse)`, qui
  régénère le PDF. La proposition BCF reprend la mise en garde quand des règles sont douteuses.
- Audit spécifique selon le CCBIM (toujours proposé dans le menu) : le CCBIM (PDF, Word, Excel ou texte) est lu
  par `read_ccbim` (chemin local quand le serveur tourne sur le poste, document de la GED BIMData, dépôt, URL ou texte
  d'une pièce jointe), l'assistant en extrait les attendus contrôlables avec leur source (`set_ccbim_expectations` :
  exigences de données ; version IFC, nom de fichier, bâtiments, niveaux, classification, locaux, objets génériques)
  et ceux qui ne le sont pas. `run_audit('ccbim')` les présente comme hypothèses à valider, puis produit le rapport
  PDF (attendus et sources au chapitre « Hypothèses », CCBIM en référentiel) et le classeur Excel (feuille « Attendus
  du CCBIM » avec les attendus non contrôlables).
- Menu d'audit : un audit à la fois, choisi par l'utilisateur (« tous » est refusé). Premier appel de `run_audit` :
  hypothèses de l'audit (valeur retenue, défaut, plage, source), référentiels, méthode, questions à poser
  (`a_demander` : référentiel PMR, référentiel incendie, EAS) et `jeton_hypotheses` ; rien n'est calculé. Le jeton
  porte sur les paramètres présentés : un réglage modifié entre-temps donne de nouvelles hypothèses à valider. Second
  appel avec le jeton : l'audit tourne, rend son rapport PDF, les règles contrôlées sur le total
  (`regles_controlees`) et `question_suite`.

S11 (local dans l'emprise du bâtiment) : un local sans aucun mur, dalle, toiture, porte ni fenêtre à moins de 0,30 m est
hors du bâtiment (copie égarée, local à supprimer ou à déplacer) ; le message donne la distance au bâti le plus proche.

S4 (élément posé sur son étage) : élément posé — étage d'assise = le plus haut dont le niveau est sous la base + 0,35 m ;
élément porté (poutres, IfcMember, IfcPlate, toitures, plafonds, réseaux) — jugé sur son dessus : anomalie s'il est tout
entier à plus de `carried_tolerance_m` (0,35 m, à augmenter sous une dalle épaisse) sous son niveau ; les réseaux dans ce
cas (souvent rattachés par Revit à l'étage du dessus) forment une seule ligne mineure par étage ; une suite d'au moins 5
éléments du même type dont les dessus montent d'un pas régulier reçoit l'indice « rampe probable, à modéliser en
IfcRamp » ; dalle — dessus près du niveau (− 0,35 / + 0,10 m) ou base posée sur son étage ; fondations
sous l'étage le plus bas rattachées à cet étage : conformes ; étages à moins de 0,70 m : « non concluant » pour l'élément
seul. Synthèse par étage quand ≥ 30 % de ses éléments posés le sont au même écart (niveau d'étage probablement décalé).
Sorties : un étage sans sortie détectée est « non contrôlé » s'il a des locaux, « sans objet » s'il n'en a aucun.

## Rapports PDF et paramètres des audits

Chaque contrôle et chaque audit groupé produit un **rapport PDF** à la charte BIMData (bleu ardoise, filets jaunes,
Roboto — à défaut Arial, puis DejaVu ; `MCP_REPORT_FONT_DIR` pour fournir Roboto-Regular/Bold), structuré comme un
rapport d'AMO / BIM manager : page de garde ; 1. Synthèse (avis global, chiffres clés, graphique par sévérité, points
majeurs, décisions et données attendues) ; 2. Objet, périmètre et documents d'entrée ; 3. Hypothèses et paramètres
de calcul (valeur retenue, défaut, plage, source ; valeurs modifiées et à fournir surlignées) ; 4. Référentiels et
textes applicables ; 5. Méthode ; 6. Résultats détaillés (règles et couverture, principales anomalies, points
d'attention) ; 7. Recommandations et plan d'actions (priorité, action, responsable) ; 8. Limites, réserves et
avertissement ; annexe (extrait des anomalies). Le classeur Excel reste l'annexe (`livrable.annexe_excel`), seul
support des corrections par fichier. Avertissement IA sur la page de garde, en pied de chaque page et au chapitre 8.

Livraison des rapports : le PDF est **joint au résultat de l'outil** (ressource `application/pdf`, jusqu'à 6 fichiers
de 20 Mo) ; sur un serveur local, une copie est rangée dans `~/Documents/BIMData MCP/Rapports` (`MCP_REPORTS_DIR`,
`0` pour désactiver) et son chemin est donné (`chemin_local`). En stdio (Claude Desktop, Claude Code), il n'y a pas de
serveur web : les liens sont des `file://` vers ce fichier, jamais des liens `http://127.0.0.1` morts. En HTTP, liens
signés et expirants comme avant.

Paramètres : `audit_params.PARAMS` (valeur par défaut, plage, pas, unité, justification, source) pour chaque audit.
`list_audits` et `audit_parameters` les listent avec la valeur retenue pour la maquette et le lien d'une **page de
réglage à barres glissantes** (`reglages_url`, lien signé et expirant servi par le serveur) ; `set_audit_parameters`
les modifie en conversation (plage et choix contrôlés). Les réglages sont enregistrés par maquette et appliqués par
`run_audit` comme par chaque outil lancé seul (`run_structure_check`, `run_clash_check`, `run_clash_matrix`,
`run_geometry_quality_check`, `run_accessibility_check`, `run_clearance_check`, `run_fire_check`) : valeur explicite,
sinon réglage enregistré, sinon défaut. Le rapport le signale (« Réglages enregistrés appliqués ») et reprend les
valeurs au chapitre « Hypothèses ». Le référentiel PMR et le
référentiel incendie n'ont pas de valeur par défaut : l'audit est demandé tant qu'ils ne sont pas fournis.

## Topics BCF depuis un contrôle (méthode Solibri, adaptée au serveur BIMData)

Après l'envoi des topics, la réponse (ou le résultat de `job_status`) propose un **rapport Excel des topics**
(`proposition_rapport` → `export_bcf_report(plan_id)`) : synthèse par priorité, règle, niveau et statut, puis un topic
par ligne avec sa miniature, son constat, sa mesure et son seuil, ses éléments (nom, classe, rôle, GlobalId), sa clé
d'anomalie et son GUID. Vocabulaire du projet BCF : un équivalent existant est utilisé tel quel (« Opened » pour Open,
« Closed » pour Resolved, « Error » pour Issue…), sans rien déclarer ; les adaptations restantes tiennent en une ligne
en fin de description.

Opérations longues : au-delà de 12 topics avec images, `prepare_bcf_topics` part en **tâche de fond** et répond tout
de suite avec un `job_id` ; au-delà de 10 éléments, `apply_plan` aussi. `job_status(job_id)` donne l'avancement (fait /
total, temps restant estimé) puis le résultat — le délai d'attente du client MCP n'est plus un problème. Images rendues
par plusieurs processus (`MCP_RENDER_WORKERS`, 4 au plus ; 40 images en 4 s au lieu de 8 sur la maquette test), topics
envoyés par 4 requêtes simultanées. Envoi **reprenable** : chaque élément envoyé est marqué dans le plan, une nouvelle
`apply_plan` ne renvoie que le reste, un topic déjà présent (même GUID) n'est pas dupliqué, un second envoi du même plan
pendant le premier est refusé.

Après chaque rapport de contrôle (outil seul ou `run_audit`), la réponse porte une question de suite
(`question_suite`) : **topics BCF** ou **Smart View** (`proposition_smart_view` : éléments en anomalie colorés par
criticité, `prepare_smart_view(check_id)`). La **proposition de topics BCF** (`proposition_bcf`) donne : nombre de topics, contenu (un topic par
anomalie pour les conflits, la structure, la géométrie, PMR, dégagements et incendie ; un par règle ailleurs), appel
prêt (`prepare_bcf_topics` et ses arguments) et question à poser. Au-delà de 60 anomalies, la proposition porte d'abord
sur les bloquantes et majeures (`severities`). Rien n'est préparé sans accord, rien n'est envoyé sans `apply_plan`.

**Ouverture dans le viewer BIMData** : le point de vue porte l'origine `ifc3d` (fenêtre 3D) et la maquette ; un seul
point de vue est illustré par topic (`extra_viewpoint` vaut `none` par défaut ; une vue complémentaire demandée reste
sans image). Le viewer applique alors directement maquette, caméra, sélection et repères. Avec plusieurs vues
illustrées ou une autre origine, il affiche « Les informations de mise en page du sujet BCF ne sont pas disponibles ».
Les topics créés avant cette correction se réparent avec `repair_bcf_topics_for_viewer` (plan, puis `apply_plan`) :
origine corrigée, vue complémentaire illustrée supprimée, titre, description, commentaires et statuts conservés.

**Smart Views depuis un contrôle** : sans filtre, dès deux règles ou plus de 30 éléments en anomalie,
`prepare_smart_view(check_id)` ne prépare rien et renvoie `etape: choix_selection_smart_view` : répartition par
gravité, règle, niveau et type de conflit, options prêtes (recommandée : bloquants et majeurs des règles fiables ;
une Smart View par règle ou par niveau via `split_by` ; une gravité, une règle, un type ; tout le contrôle avec les
conformes en vert via `perimetre_valide=True`). Filtres : `severities`, `rules`, `types`, `storeys`.

**Choix du périmètre avant l'envoi** : depuis un contrôle, au-delà de 20 topics sans filtre, `prepare_bcf_topics` ne
prépare rien et renvoie `etape: choix_perimetre_bcf` : répartition des topics par gravité, par règle (avec la
fiabilité de la règle) et par type de conflit, options prêtes (recommandée en premier : bloquants et majeurs des
règles fiables ; puis une gravité, une règle, un type ; « tout envoyer » via `perimetre_valide=True`). Filtres
combinables : `severities`, `rules` (A5, S4, CLASH…), `types` (doublon, inclus, chevauchement, ou case de matrice).
Une anomalie hors filtre n'est jamais proposée à la clôture.

`prepare_bcf_topics(check_id=…)` : chaque topic porte un point de vue complet, comme une diapositive d'anomalie
Solibri ; rien n'est écrit avant `apply_plan(confirm=True)`. Cible **BCF 2.1** (seule version acceptée par BIMData, la
plus sûre pour Solibri) : tout ce qui doit arriver dans Solibri passe par un champ standard ; les champs propres à
BIMData (`pins`, `models`) ne servent qu'à l'affichage dans BIMData et ne remplacent jamais un champ standard.
- Topic : `title` « [règle] résumé — niveau » ; `description` (règle, mesure, seuil, éléments avec nom, type, GlobalId
  et rôle, check_id, date du fichier IFC, ligne « clé : <clé> ») ; `guid` fourni, dérivé de la clé (UUID v5 sur projet
  + maquette + clé : même anomalie, même GUID à chaque relance) ; `reference_links` = URN
  `urn:mcp-bimdata:<règle>:<GlobalId triés>` ; `models` = identifiant de la maquette (jamais `ifcs`) ; `format`
  « standard ».
- Extensions du projet (types, priorités, statuts, libellés) : lues avant l'envoi ; les valeurs manquantes (type
  Clash / Issue, priorités Critical, High, Low, Information, statuts Open et Resolved, libellés famille, règle, niveau)
  sont déclarées par `POST …/extension/{type|priority|status|label}` en tête du plan — réglage du projet, pas du
  serveur. Déclaration refusée (droits) : valeur existante la plus proche, valeur souhaitée écrite dans la description.
- Point de vue : caméra perspective ou orthogonale avec ses seuls champs standard (méthode de cadrage documentée dans
  `bcf.py`) ; `clipping_planes` (boîte de 6 plans à normales vers l'extérieur, ou plan à l'étage + 1,20 m normale vers
  le haut) ; `lines` = chemin F3 / F5 et une croix de 3 segments de 0,5 m au centre de la zone (le repère tel que
  Solibri le relit) ; `pins` (point, nom = règle, couleur `ff0000`) pour BIMData ; `guid` fourni (commentaires liés).
- Composants : `ifc_guid` pour chaque élément (c'est lui que Solibri utilise), `authoring_tool_id` = identifiant de la
  maquette (comme les Smart Views), couleurs par rôle (FF0000 A / cible, FFA500 B / obstacle, 0000FF contexte),
  visibilité explicite, `view_setup_hints` à faux ; au-delà de 1 000 éléments, topic découpé par étage puis par lots.
- Image PNG 1 280 × 720 par point de vue, rendue côté serveur (Pillow, sans carte graphique) avec la même caméra, les
  mêmes couleurs et la même coupe ; miniatures dans `show_plan`.
- Coordonnées dans le repère de l'IFC, en mètres, **sans décalage** : un écart affiché par le visualiseur BIMData est une
  limite du visualiseur, à signaler, pas à compenser (`MCP_BCF_VIEWER_OFFSET` reste à 0).
- Relance d'un contrôle : topic retrouvé par son GUID, sinon par `reference_links` (ou la ligne « clé : ») ; commentaire
  « toujours présent » lié à un nouveau point de vue si la vue a changé ; statut Resolved proposé pour une anomalie
  disparue. Envoi : une nouvelle tentative par topic (sans `pins`), puis on continue ; compte rendu par clé.
- À valider en recette sur un projet de test : aller-retour de 5 topics (GUID conservé, reference_links relu,
  extensions acceptées, image et repère affichés), sens de la coupe et coordonnées, export BCF-XML ouvert dans Solibri,
  relance sans doublon ; limite de taille d'une requête full-topic (découpage à 1 000 éléments).

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
ajout de la propriété au pset, sinon création du pset.

Classifications (`système:code`, ex. `uniformat:D5010200`) : code cherché dans la maquette
(`GET …/model/{id}/classification`) ou créé, associé en bulk (`POST …/classification-element`), puis l'ancien code
du même système est retiré (`DELETE …/element/{uuid}/classification/{id}`) — une association n'en remplace pas une
autre. Les autres systèmes sont conservés.

Les champs spatiaux, matériaux et classes IFC sont contrôlés mais **non corrigés** par l'API (à corriger dans le
logiciel auteur). Toute correction écrite dans BIMData (Pset, classification) doit aussi être reportée sur le type
dans le logiciel auteur, sinon elle est écrasée au prochain export IFC.

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
5. Points de vue BCF (recettes 1 à 3 sur un projet de test, écriture dans BIMData) : voir la section « Topics BCF ».

## Pistes

* Remonter dans `bimdata-write` les routes ajoutées ici (PATCH propriété, GED, navigation).
* Corriger `bim_query.views` : l'arbre spatial BIMData type ses nœuds en *snake_case* (`storey`, `space`), d'où des
  étages / pièces toujours vides côté `bim-query` (contourné ici par `model_view.py`).
* Plusieurs instances : partager `/data` (ou passer l'état sur Redis / PostgreSQL).
