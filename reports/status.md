# idea-deu status

## Source

- build: `261.25134.95`
- hash: `71b0e287a2fec5fe3428dda95ad8e947e4c35cd35e7dd3e5cad1fc19dc92fb3e`
- version: `2026.1.3`

## Counts

- Resource files: 4192
- Translation units: 72142

## Statuses

| Status | Count |
|---|---:|
| open | 3875 |
| translated | 299 |
| technically_reviewed | 67968 |
| linguistically_reviewed | 0 |

## Exclusions

| Reason | Count |
|---|---:|
| already_localized | 3 |
| collision_not_selected | 20 |
| directory | 2306 |
| localized | 1273 |
| nested_archive | 40 |
| not_in_translation_reference | 945 |
| not_jar | 1942 |
| unsupported_resource | 587662 |

## Findings and collisions

- Blocking findings: 2103
- Warning findings: 313
- Collisions: 2 (0 unresolved)

| Finding code | Count |
|---|---:|
| empty_target | 1374 |
| length_ratio | 313 |
| markup_structure_changed | 228 |
| message_format_invalid | 8 |
| placeholder_mismatch | 493 |

## Workflow

- Last completed batch: 9
- Current batch: none
- Stale units: 0
- Workflow state: `translate`
- Generated: present=False, valid=False (`generated/plugin`)
- Package: present=False, valid=False, sha256=`unavailable`, size=unavailable (`dist/idea-deu.zip`)

Next command:

`python -m scripts.idea_deu next-batch --limit 100`
