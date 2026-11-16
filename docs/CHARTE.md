# Charte des livrables — BIMData MCP

Tous les fichiers produits par le serveur sont construits par **un seul module**,
`src/bimdata_mcp/charte.py` (classe `Livrable`). Aucune sortie Excel n'est écrite autrement :
c'est ce qui garantit qu'un export, un COBie, un rapport de contrôle ou un gabarit ont la même trame.

## Identité (BIMData Brand Guidelines 2022 v1.0, tokens `bim_reporting.theming`)

| Usage | Valeur |
|---|---|
| Police | Roboto (repli Arial) |
| Primary — titres, en-têtes de tableaux | `#2F374A` |
| Secondary — filets, ligne de groupes, cellules à saisir (teinte) | `#F9C72C` |
| Blue Neutral Light — zébrage, blocs d'identification | `#F0F5FF` |
| Granite — texte secondaire | `#606060` |
| États : conforme / avertissement / non conforme | `#00AF50` / `#FF9100` / `#FF3D1E` (teintes claires en fond de cellule) |
| Criticité : bloquant / majeur / mineur / info | High / Warning / Secondary / Royal blue `#3375DD` |
| Logo | `assets/bimdata_logo.png` (Synthèse, en-tête d'impression) |

## Trame commune

1. **Ordre des feuilles** : `Synthèse` → feuilles de données → `Lisez-moi` → `_bimdata` (masquée).
2. **Synthèse** : logo, surtitre `BIMDATA — <TYPE>`, titre souligné d'un filet jaune, bloc d'identification
   **toujours dans cet ordre** : Projet, Maquette, Identifiants BIMData, Périmètre, Phase, Généré le, Généré par,
   Outil ; puis « Chiffres clés » (6 au plus) ; puis tableaux de synthèse.
3. **Tableaux** : ligne 1 = groupes (jaune), ligne 2 = en-têtes (Primary, texte blanc), lignes zébrées, filtres,
   volets figés, formats `#,##0` / `#,##0.000` / `0.0%`, booléens `TRUE` / `FALSE`.
   Exception justifiée : feuilles COBie et gabarit d'import, en-tête sur une seule ligne (format attendu par les
   outils tiers et par l'import).
4. **Couleurs de cellule** : jaune clair = à saisir (relu à la réimport) ; vert / orange / rouge = états ;
   gris = non applicable.
5. **Onglets** : Primary = synthèse, Royal blue = données, Secondary = à saisir, Granite = référence,
   Silver = aide.
6. **Lisez-moi** : objet du document, sommaire des feuilles (généré), sections propres au livrable, légende commune.
7. **Impression** : A4 paysage, ajusté en largeur, en-têtes répétés, logo en en-tête, « BIMData.io — titre » et
   « page n / N » en pied.
8. **Nom de fichier** : `AAAAMMJJ_<projet>_<maquette>[_<complément>]_<TYPE>.xlsx`
   (`EXPORT`, `COBIE`, `CONTROLE`, `GABARIT-CDC`).
9. **Métadonnées** (`_bimdata`) : format versionné, cible, date, générateur — utilisées pour la réimport.

## Réponses des outils

Chaque outil qui produit un fichier renvoie la même structure :

```json
{"livrable": {"type", "titre", "fichier", "file_id", "download_url", "taille_ko", "feuilles"},
 "synthese": {...}, "avertissements": [...], "suite": [...]}
```

et les instructions du serveur imposent à Claude la même trame de réponse : périmètre → chiffres clés →
livrable → points d'attention → suite proposée.

## Ajouter un nouveau livrable (ex. rapport)

```python
lv = Livrable(path, Contexte(type_livrable="Rapport", titre=..., projet=..., maquette=..., cible=...))
lv.synthese(kpis=[...], intro="...")
lv.tableau("...", [Col(...)], rows)
lv.feuille("...", cols, rows, description="...")
lv.lisez_moi("Objet…", [("Section", ["…"])])
lv.meta({"format": "bimdata-mcp/<type>/v1"})
lv.close()
```

Un futur rapport Word / PDF devra reprendre les mêmes éléments (bloc d'identification, chiffres clés, légende)
avec les primitives Word de `bim-reporting`.
