# Deutsches Sprachpaket für IntelliJ IDEA 2025.3.1.1

## Ziel

Das Projekt erzeugt ein möglichst vollständiges deutsches Sprachpaket für die einheitliche IntelliJ-IDEA-Distribution 2025.3.1.1. Als verbindliche Quelle dient das Windows-ZIP mit der Produktversion `2025.3.1.1` und der Buildnummer `IU-253.29346.240`.

Das Ergebnis ist ein offline installierbares Plugin-ZIP. Es wird außerhalb des Air-Gapped-Netzes erstellt und anschließend über **Install Plugin from Disk** eingespielt. Im abgeschotteten Netz benötigt das Plugin keinen Netzwerkzugriff.

Das Sprachpaket gilt ausschließlich für Build `253.29346.240`. Für einen anderen IDE-Build muss ein neuer, differenzbasierter Übersetzungslauf und ein neues Plugin-Artefakt entstehen.

## Rahmenbedingungen

- Seit IntelliJ IDEA 2025.3 gibt es eine einheitliche Distribution für den kostenlosen und den lizenzpflichtigen Funktionsumfang. Daher wird ein einziges Sprachpaket erzeugt.
- Die Windows-Distribution ist die Ressourcenquelle. Die übersetzten Java-Ressourcen und das erzeugte Sprachpaket sind grundsätzlich plattformunabhängig.
- Die Übersetzung wird außerhalb des Air-Gapped-Netzes erstellt; nur das fertige Artefakt wird eingeschleust.
- Codex erstellt die deutschen Übersetzungen dateiweise und in wiederaufnehmbaren Batches.
- Die Übersetzung formuliert möglichst neutral. Ist eine direkte Ansprache unvermeidbar, verwendet sie „Sie“.
- Das kostenpflichtige deutsche Marketplace-Sprachpaket ist keine Abhängigkeit und wird nicht vorausgesetzt.

## Lösungsansatz

Ein Generator liest alle unterstützten englischen Ressourcen aus der unveränderten IntelliJ-Distribution, inventarisiert sie und erzeugt deutsche Gegenstücke mit den von der IntelliJ Platform erwarteten Ressourcenpfaden. Ein minimales Language-Pack-Plugin registriert die Locale `de` über `com.intellij.languageBundle`.

Die Originaldistribution bleibt unangetastet. Ein direktes Patchen der IDE-JARs ist ausdrücklich ausgeschlossen, da es Updates, Reproduzierbarkeit und Integritätsprüfungen beeinträchtigen würde.

## Komponenten

### Quellprüfung

Vor jedem Lauf prüft die Pipeline:

- Dateiname und Vorhandensein des IntelliJ-ZIP;
- `product-info.json` mit Version und Buildnummer;
- SHA-256-Prüfsumme des vollständigen ZIP.

Die erwartete Prüfsumme wird in der Projektkonfiguration festgehalten. Ein abweichendes Archiv stoppt den Lauf mit einer verständlichen Fehlermeldung.

### Ressourcen-Scanner

Der Scanner durchsucht die IDE-JARs und die JARs gebündelter Plugins. Er erfasst die von IntelliJ-Sprachpaketen unterstützten Ressourcentypen:

- Message Bundles (`*.properties`);
- Inspektionsbeschreibungen (`inspectionDescriptions/**/*.html`);
- Intentionsbeschreibungen (`intentionDescriptions/**/*.html`);
- Beschreibungen von Datei-Templates (`fileTemplates/**/*.html`);
- Beschreibungen von Postfix-Templates (`postfixTemplates/**/*.xml`);
- „Tip of the Day“-Dateien (`tips/**/*.html`).

Jeder Inventareintrag enthält mindestens Quell-JAR, Ressourcenpfad, Ressourcentyp, Quell-Prüfsumme und Bearbeitungsstatus. Bereits lokalisierte Dateien, binäre Inhalte, Drittanbieter-Bibliotheken ohne sichtbare IntelliJ-Oberfläche und technische Metadaten werden ausgeschlossen. Ausschlussregeln sind explizit konfiguriert und werden im Scanbericht ausgewiesen, damit „vollständig“ messbar bleibt.

Ressourcenpfade, die in mehreren Quell-JARs vorkommen, werden als Kollisionen erfasst. Der Paketbau darf solche Ressourcen erst übernehmen, wenn ihre Zuordnung anhand der IntelliJ-Lookup-Regeln oder der Struktur eines kompatiblen Sprachpakets eindeutig geklärt ist.

### Übersetzungsdatenbank

Übersetzungen werden als kleine, menschenlesbare JSONL-Batches gespeichert. Jeder Datensatz enthält:

- stabile Ressourcenkennung;
- englischen Quelltext und dessen Prüfsumme;
- deutschen Zieltext;
- notwendigen Kontext wie Bundle, Schlüssel und Ressourcenpfad;
- Status `offen`, `übersetzt`, `technisch_geprüft` oder `sprachlich_geprüft`;
- technische Prüfbefunde.

Ein Lauf verarbeitet nur Einträge im passenden Status. Nach jedem Batch werden Daten atomar geschrieben: zunächst in eine temporäre Datei, dann per Umbenennung. Dadurch bleiben abgeschlossene Übersetzungen auch bei Token-, Prozess- oder Zeitlimits erhalten.

### Glossar und Stilregeln

Ein versioniertes Glossar legt die bevorzugte Terminologie fest. Etablierte Fachbegriffe wie Git, Commit, Branch, Debugger und Breakpoint bleiben erhalten, sofern der konkrete UI-Kontext keine andere Form verlangt.

Die Übersetzung folgt diesen Regeln:

- neutral formulieren; nur bei Bedarf mit „Sie“ ansprechen;
- kurze, natürliche UI-Texte statt wörtlicher und sperriger Übertragungen;
- gleiche englische Wörter kontextabhängig übersetzen;
- Platzhalter, Mnemonics, Tastenkürzel und technische Bezeichner unverändert erhalten;
- Produkt-, API- und Dateinamen nicht unbeabsichtigt übersetzen;
- bestehende HTML- und XML-Struktur bewahren.

### Generator und Paketierer

Der Generator rekonstruiert Properties-, HTML- und XML-Dateien in der von IntelliJ erwarteten Pfadstruktur. Das Plugin enthält keine ausführbare Programmlogik. Seine `plugin.xml` registriert `locale="de"` und begrenzt die Kompatibilität auf den Ziel-Build `253.29346.240`.

Der Paketierer erzeugt ein deterministisches Plugin-ZIP. Gleiche Eingaben müssen dasselbe Ressourceninventar und inhaltlich identische Ausgaben erzeugen. Zeitstempel oder Dateireihenfolgen dürfen keinen sachlichen Unterschied verursachen.

### Fortschrittsbericht

Nach jedem Schritt wird ein maschinen- und menschenlesbarer Bericht aktualisiert. Er enthält:

- Anzahl aller inventarisierten Dateien und Texte;
- Anzahl pro Bearbeitungsstatus;
- technisch oder sprachlich auffällige Einträge;
- ausgeschlossene Ressourcen mit Grund;
- Pfadkollisionen;
- zuletzt vollständig abgeschlossenen Batch;
- nächsten ausführbaren Arbeitsschritt.

Der Bericht ist zusammen mit den persistierten JSONL-Batches ausreichend, um die Arbeit in einer neuen Sitzung ohne Gesprächskontext fortzusetzen.

## Datenfluss

1. Quell-ZIP validieren und fingerprinten.
2. IDE- und Plugin-JARs inventarisieren.
3. Unterstützte Ressourcen extrahieren und normalisieren.
4. Neue oder geänderte Quelltexte als `offen` markieren.
5. Offene Einträge in begrenzten Batches übersetzen.
6. Übersetzungen technisch prüfen und Befunde speichern.
7. Auffällige oder mehrdeutige Texte sprachlich prüfen.
8. Deutsche Ressourcen erzeugen.
9. Vollständigkeit und Paketstruktur prüfen.
10. Plugin-ZIP bauen, verifizieren und in der Ziel-IDE testen.

Bei einem späteren IntelliJ-Build vergleicht der Scanner Ressourcenkennungen und Quell-Prüfsummen. Unveränderte Texte behalten ihre Übersetzungen und Status. Neue oder geänderte Texte werden `offen`; entfernte Texte werden als veraltet gemeldet und nicht mehr paketiert.

## Fehlerbehandlung und Qualitätsprüfungen

Folgende Fehler blockieren den Paketbau:

- fehlende oder zusätzliche Platzhalter;
- beschädigte Java-`MessageFormat`-Syntax;
- ungültige Properties-Escapes oder nicht lesbare Zeichencodierung;
- veränderte oder unausgeglichene HTML-/XML-Tags;
- unbeabsichtigt veränderte Links;
- doppelte Schlüssel;
- leere Übersetzungen;
- ungeklärte Ressourcenpfad-Kollisionen;
- nicht klassifizierte Lücken gegenüber dem Inventar;
- Quell-ZIP oder Buildnummer weichen von der Konfiguration ab.

Warnungen statt Blockaden entstehen unter anderem bei stark abweichender Textlänge, mehrdeutigen Einzelwörtern, inkonsistenter Glossarnutzung und noch nicht sprachlich geprüften Texten. Sie werden in priorisierten Stichprobenlisten gesammelt.

## Verifikation und Abnahmekriterien

Die Lösung ist für Build `253.29346.240` abnahmefähig, wenn:

1. das gesamte Quell-ZIP reproduzierbar inventarisiert wurde;
2. jede unterstützte sichtbare Ressource übersetzt oder mit nachvollziehbarem Ausschlussgrund dokumentiert ist;
3. alle blockierenden technischen Prüfungen erfolgreich sind;
4. das Plugin-ZIP von den IntelliJ-Plugin-Prüfwerkzeugen akzeptiert wird;
5. IntelliJ IDEA 2025.3.1.1 mit installiertem Sprachpaket startet;
6. Deutsch in **Language and Region** auswählbar ist und nach einem Neustart aktiv bleibt;
7. Kernabläufe für Projektöffnung, Editor, Navigation, Suche, Einstellungen, Build, Run/Debug, Git und lizenzpflichtige Funktionen stichprobenartig geprüft wurden;
8. eine nicht lizenzierte und eine lizenzierte Nutzung der einheitlichen Distribution keine fehlenden Plugin-Abhängigkeiten erzeugen;
9. der Abschlussbericht Übersetzungsgrad, Ausschlüsse und verbleibende sprachliche Warnungen ausweist;
10. die Offline-Installation aus dem erzeugten ZIP ohne Marketplace-Zugriff funktioniert.

## Projektstruktur

```text
config/                Buildbindung und Scannerregeln
inventory/             Ressourceninventar und Prüfsummen
glossary/              Terminologie und Stilregeln
translations/          fortsetzbare JSONL-Batches
generated/             lokal erzeugte Ressourcen, nicht versioniert
reports/               Fortschritts- und Qualitätsberichte
dist/                  lokal erzeugte Plugin-ZIPs, nicht versioniert
scripts/               Scanner, Prüfer, Generator und Paketierer
docs/superpowers/specs/ Designspezifikationen
```

Das IntelliJ-Distributions-ZIP, `generated/` und `dist/` werden nicht in Git aufgenommen. Versioniert werden Konfiguration, Glossar, Übersetzungsdaten, Skripte, relevante Berichte und Dokumentation.

## Referenzen

- [JetBrains: Providing Translations](https://plugins.jetbrains.com/docs/intellij/providing-translations.html)
- [JetBrains: IntelliJ IDEA Unified Distribution](https://blog.jetbrains.com/idea/2025/07/intellij-idea-unified-distribution-plan/)

