# idea-deu status

## Source

- build: `261.26222.65`
- hash: `5c92bc8dcca7b39857e7b24029432b558cac61ed518761e3b824590b849d548a`
- version: `2026.1.4`

## Counts

- Resource files: 4191
- Translation units: 71476

## Statuses

| Status | Count |
|---|---:|
| open | 0 |
| translated | 0 |
| technically_reviewed | 71476 |
| linguistically_reviewed | 0 |

## Exclusions

| Reason | Count |
|---|---:|
| already_localized | 3 |
| collision_not_selected | 22 |
| directory | 2306 |
| localized | 1273 |
| nested_archive | 40 |
| not_in_translation_reference | 945 |
| not_jar | 1942 |
| unsupported_resource | 587985 |

## Findings and collisions

- Blocking findings: 0
- Warning findings: 332
- Collisions: 0 (0 unresolved)

| Finding code | Count |
|---|---:|
| length_ratio | 332 |

## Workflow

- Last completed batch: 30
- Current batch: none
- Stale units: 0
- Workflow state: `generate`
- Generated: present=True, valid=False (`generated/plugin`)
- Package: present=True, valid=False, sha256=`188ddf6b144e2fb5c92462fabbe95a519da518af0854b9eecea9236a639a4823`, size=2782881 (`dist/idea-deu.zip`)

Next command:

`python -m scripts.idea_deu generate`
