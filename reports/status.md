# idea-deu status

## Source

- build: `253.29346.240`
- hash: `755b9549eb41ddec86ea111e4aba94b4fd6e39a60b1de7945c92652c70f80026`
- version: `2025.3.1.1`

## Counts

- Resource files: 4147
- Translation units: 72513

## Statuses

| Status | Count |
|---|---:|
| open | 63197 |
| translated | 18 |
| technically_reviewed | 9298 |
| linguistically_reviewed | 0 |

## Exclusions

| Reason | Count |
|---|---:|
| already_localized | 3 |
| collision_not_selected | 20 |
| directory | 2264 |
| localized | 1271 |
| nested_archive | 38 |
| not_in_translation_reference | 1003 |
| not_jar | 1898 |
| unsupported_resource | 577039 |

## Findings and collisions

- Blocking findings: 82410
- Warning findings: 31
- Collisions: 3 (0 unresolved)

| Finding code | Count |
|---|---:|
| empty_target | 63197 |
| length_ratio | 31 |
| markup_structure_changed | 5509 |
| message_format_invalid | 1 |
| placeholder_mismatch | 13703 |

## Workflow

- Last completed batch: 73
- Current batch: none
- Stale units: 0
- Workflow state: `translate`
- Generated: present=False, valid=False (`generated/plugin`)
- Package: present=False, valid=False, sha256=`unavailable`, size=unavailable (`dist/idea-deu.zip`)

Next command:

`python -m scripts.idea_deu next-batch --limit 100`
