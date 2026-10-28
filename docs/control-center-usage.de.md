# Die Steuerzentrale benutzen

Öffne die Adresse deines Drucker-Pis im Browser. Die Übersicht zeigt Verbindung, gemeldete Tintenfüllstände und die Druckwarteschlange. Auch bei ausgeschaltetem Drucker bleibt die Oberfläche erreichbar. Oben kannst du Deutsch, Englisch oder die automatische Sprachwahl einstellen.

## Anmelden und Dokumente hinzufügen

Melde dich mit deinem eigenen Control-Center-Passwort an. Erst danach sind persönliche Dokumente und Bedienaktionen erreichbar. Die Ersteinrichtung des Passworts steht in der [Installationsanleitung](../packaging/README.md#control-center-documents-and-password).

Unter **Drucken** oder **Dokumente** kannst du PDF-, PNG- und JPEG-Dateien hinzufügen. Eine Datei darf höchstens 20 MiB und ein Dokument höchstens 25 Seiten enthalten. Verschlüsselte PDFs werden abgewiesen. Die Vorschau wird im Hintergrund erstellt.

## Drucken

Wähle das Dokument und danach Seiten, Kopienzahl, Papierformat, Material, Qualität sowie Farbe oder Graustufen. Bei Bildern legt **Einpassen** das vollständige Bild auf das Papier; **Ausfüllen** nutzt die ganze Fläche und kann Bildränder abschneiden. Die Materialauswahl begrenzt die verfügbaren Qualitätsstufen.

Kontrolliere die Zusammenfassung und bestätige **Drucken**. Erst diese Bestätigung erzeugt einen Druckauftrag. Ein Neuladen der Seite startet keinen weiteren Druck. Die Auftragsanzeige erlaubt das Abbrechen. „Abgeschlossen“ bezeichnet den gemeldeten Zustand der Druckverwaltung; die Papierausgabe lässt sich anschließend am Gerät prüfen.

## Scannen und Kopieren

Schalte den Drucker ein und lege das Original auf das Vorlagenglas. **Scanner prüfen** sucht ausdrücklich nach diesem Modell und liest die angebotenen Einstellungen. Die Auswahl zeigt anschließend die gemeldeten Auflösungen und Farbmodi. Erst **Scannen** startet die Aufnahme. Bei fehlender Verbindung erscheint eine Erklärung.

**Schnelle Vorschau** liest das gesamte gemeldete Vorlagenglas mit der niedrigsten unterstützten Auflösung ein. Ziehe auf dem Vorschaubild ein Rechteck oder trage Position und Größe unter **Scanbereich auswählen** in Millimetern ein. Der folgende Scan verwendet diesen Bereich mit der separat gewählten Auflösung. **Gesamtes Vorlagenglas verwenden** setzt die Auswahl zurück. Wenn du die Vorlage verschiebst, fordere eine neue Vorschau an. Gespeicherte Vorschauen bleiben nach einem Browserneustart verfügbar; eine neue Aufnahme wird dabei nicht automatisch gestartet.

Der gewählte Bereich wird auch in Scan-Profilen und mehrseitigen Scan-Dokumenten gespeichert. Ein Wert außerhalb der vom Scanner gemeldeten Grenzen wird vor der Aufnahme abgewiesen.

Für mehrere Seiten legst du unter **Mehrseitiges Dokument** ein neues Dokument an. **Erste Seite scannen** übernimmt die erste Vorlage. Danach tauscht du die Vorlage aus und wählst **Weitere Seite scannen**. Die Seitenliste bleibt auf dem Pi gespeichert; nach dem Schließen oder Neuladen des Browsers kannst du dasselbe Dokument fortsetzen. Farbmodus und Auflösung werden dabei wiederhergestellt.

Mit **Nach vorne**, **Nach hinten**, **Drehen oder zuschneiden** und **Seite entfernen** ordnest du die Seiten vor dem Abschluss. **Fertige PDF speichern** erstellt eine gemeinsame Datei mit bis zu 25 Seiten. Auf Wunsch läuft dabei direkt die Texterkennung mit. Ein Abbruch lässt das Scan-Dokument zur weiteren Bearbeitung offen. Entfernen einer Seite oder eines Scan-Dokuments löscht die Originaldateien nicht; sie bleiben in **Dokumente** und unterliegen dort der eingestellten Aufbewahrung. Solange ein Scan-Dokument noch auf seine Originale verweist, sind sie vor der automatischen Löschung geschützt. Es wird kein automatischer Vorlageneinzug vorausgesetzt.

**Kopieren** liest zunächst das Original ein und erzeugt daraus einen Druckauftrag. Wenn das Einlesen scheitert oder abgebrochen wird, beginnt kein Druck. Die Scanner- und Kopierpfade benötigen einen passenden nativen Scanner-Treiber. Die erste Aufnahme am DCP-150C und der Wechsel zwischen Scannen und Drucken bleiben eigenständige Gerätetests.

Für einen Ausschnitt aktiviere unter **Kopieren** ausdrücklich **Nur den unter Scannen gewählten Bereich kopieren**. Ohne diese Auswahl wird der normale Dokumentbereich verwendet. Die Bestätigung zeigt den gewählten Bereich an. Über **Bild auf der Seite** wählst du Einpassen oder Ausfüllen des Papiers.

## Dokumente und Texterkennung

Ein Dokument lässt sich herunterladen, umbenennen, behalten oder löschen. Unter Bearbeiten wählst du die Seitenfolge, Drehung und bei Bedarf einen Ausschnitt. Die Bearbeitung erstellt eine eigene PDF-Datei und lässt die Ausgangsdatei verfügbar. Mehrere ausgewählte Dokumente lassen sich zu einer mehrseitigen PDF zusammenfügen.

**Texterkennung** verarbeitet das gewählte Dokument lokal mit Tesseracts neuronaler LSTM-Erkennung. Wähle die Sprache des Inhalts; Deutsch und Englisch können gemeinsam erkannt werden. Du erhältst eine durchsuchbare PDF und eine Textdatei. Das läuft seitenweise im Hintergrund und lässt sich abbrechen. Auf einem kleinen Pi kann die Verarbeitung länger dauern. Prüfe Zahlen und wichtige Angaben im erkannten Text.

Dokumente werden standardmäßig nach 24 Stunden gelöscht. **Behalten** schützt eine Datei vor dieser automatischen Löschung. In den Einstellungen kannst du Löschfrist und Speicherlimit ändern. Herunterladen oder Teilen verwendet die normale Funktion deines Browsers beziehungsweise Handys; die Steuerzentrale verschickt selbst keine E-Mails.

## Profile und Einstellungen

Profile merken benannte Einstellungen für Drucken, Scannen, OCR oder Kopieren. Das Anwenden eines Profils füllt das entsprechende Formular aus und startet keine Geräteaktion. Ein Profil ersetzt nicht die Prüfung, ob eine Einstellung am verbundenen Gerät verfügbar ist.

Der Einstellungsexport enthält Profile und Aufbewahrungseinstellungen. Passwörter, Anmeldesitzungen und Dokumentinhalte gehören nicht dazu. Eine Anmeldung läuft spätestens nach zwölf Stunden ab. Melde dich an gemeinsam genutzten Geräten wieder ab.

## Dokumente sichern und wiederherstellen

Markiere die gewünschten Dateien unter **Dokumente** und wähle **Ausgewählte Dokumente sichern**. Unter **Einstellungen → Sichern und wiederherstellen** kannst du Profile und Einstellungen einschließen und die Sicherung vorbereiten. Der ZIP-Download enthält die ausgewählten Originaldateien, ihre Namen, ursprünglichen Erstellungsdaten, Behalten-Auswahl, Scan-Metadaten und vorhandenen OCR-Text. Passwörter, Anmeldesitzungen, Auftragshistorie und offene Scan-Dokumente werden nicht exportiert. Sichere zuerst eine fertige mehrseitige PDF, wenn du sie mitnehmen möchtest.

Vorbereitete Downloads bleiben eine Stunde auf dem Pi und lassen sich dort wieder entfernen. Das Entfernen betrifft nur das Archiv; seine Originaldokumente bleiben verfügbar. Lade die ZIP-Datei herunter und bewahre sie an deinem eigenen Speicherort auf. Höchstens drei Downloads werden gleichzeitig vorgehalten. Für das Erstellen wird zusätzlich freier Platz für das Archiv benötigt.

Wähle zum Wiederherstellen die ZIP-Datei und prüfe die Zusammenfassung. Das Übernehmen von Profilen und Einstellungen ist eine eigene Auswahl: Profile werden hinzugefügt, die gespeicherten Speicher- und Aufbewahrungseinstellungen ersetzen die aktuellen Werte. Alle Archivteile, Prüfsummen und tatsächlichen Dokumentformate werden vor der gemeinsamen Übernahme geprüft. Ein beschädigtes Archiv oder ein Fehler hinterlässt keine teilweise übernommenen Dateien oder Einstellungen. Die Wiederherstellung startet keine Geräteaktion.

Dokumente erhalten neue Kennungen, ihre bisherigen Namen und Erstellungsdaten bleiben erhalten. Vorhandene Dokumente werden nicht ersetzt. Temporäre Dateien erhalten ab der Wiederherstellung eine neue Aufbewahrungsfrist, **Behalten** bleibt erhalten. Vorschauen werden neu erzeugt. Eine ausdrücklich erneut gestartete Wiederherstellung erstellt weitere Kopien; ein verlorener Antwortaufruf wird dagegen unter derselben Auftragskennung wiederaufgenommen.

Das entpackte Archiv und alle vorhandenen Dokumente müssen in das aktuelle Speicherlimit passen. Große Uploads können zusätzlich durch den Fernzugang begrenzt werden; verwende dann die lokale Adresse des Drucker-Pis. Hochladen und Wiederherstellen laufen ohne das Einlesen des gesamten Archivs in den Arbeitsspeicher.

## Wartungshinweise

Unter **Einstellungen → Wartung am Drucker** findest du modellbezogene Hinweise mit Links zu den betreffenden Seiten des [offiziellen Brother-Handbuchs](https://download.brother.com/welcome/doc002242/cv_dcp135_ukeng_usr.pdf#page=72). Geräteaktionen führst du am Drucker aus.

## Noch an das Gerät gebunden

Ein bytegleicher Druckdatenvergleich bestätigt die verglichenen Druckdaten. Tatsächliche Scanneraufnahme, Gerätetasten, Kartenleser und Wartungsbefehle werden jeweils gesondert geprüft. Diese Bereiche erhalten erst nach passenden Nachweisen eine aktive Bedienung. Es werden keine Druck-, Scan- oder Reinigungsvorgänge beim Öffnen der Seite gestartet.
