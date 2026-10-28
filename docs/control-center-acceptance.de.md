# Prüfübersicht für das Control Center

Stand: 28. Oktober 2026. Das Projekt bleibt eine Vorschau. Der bestätigte physische Druck und die Softwareprüfungen haben unterschiedliche Aussagekraft. Eine vollständige Geräteabnahme ist noch offen.

| Bereich aus dem Funktionsplan | Vorhandener Nachweis | Noch am Gerät erforderlich |
| --- | --- | --- |
| Nativer Druckerkern | Verglichene Druckdaten, sechs Qualitätsstufen, ARMv6-Ausführung und bestätigter A4-Druck; siehe [Hardwaretest](hardware-validation.json) | Weitere Papier-, Qualitäts- und Mehrseiteneinstellungen |
| Druckverwaltung und Netzwerk | Eigene freigegebene Warteschlange, gemeinsame Gerätesperre, geprüfte Optionen und Linux-Netzwerkerkennung | Gesamtablauf nach Ausschalten, USB-Wiederverbindung und Neustart |
| Anmeldung und Fernzugriff | Geschützte Downloads und Aktionen, abgesicherte Anfragen, HTTPS-Anmeldung sowie tatsächlicher Upload und Download geprüft | Kein zusätzliches Gerät für die Webanmeldung nötig |
| Dokumente | Tatsächliche PDF-, PNG- und JPEG-Prüfung, Vorschau, Seitenfolge, Drehung, Zuschnitt, Zusammenführen und Aufbewahrung | Kein Gerät für vorhandene Dateien nötig |
| Texterkennung | Lokale neuronale Erkennung, mehrseitige durchsuchbare PDFs und Textdateien; leeres Ergebnis auf farbigem Hintergrund mit einmaliger lokaler Korrektur wiederhergestellt, Originalfarben und Abbruch geprüft | Qualität weiterer gescannter Vorlagen beurteilen |
| Native Scanner-Anbindung | Modellpassendes Backend und nativer Decoder gebaut und installiert; Decoder-Ausgaben auf drei Builds verglichen | Eingeschalteten DCP-150C erkennen, vollständiges Bild aufnehmen, abbrechen und danach drucken |
| Vorschau und Scanbereich | Echtes Bild aus einem isolierten virtuellen Scanner; Auswahl per Maus, Touch und Millimeterfeldern; tatsächliche Ausgabegrößen geprüft | Gemeldete Grenzen, Auflösungen und Farben am DCP-150C bestätigen |
| Mehrseitiges Scannen | Weitere Seite, Wiederaufnahme im Browser, Reihenfolge, Drehung, Zuschnitt und PDF-Abschluss mit optionaler Texterkennung geprüft | Mehrere Vorlagen tatsächlich vom Vorlagenglas einlesen |
| Kopieren | Scan-zu-Druck-Verarbeitung mit aufzeichnendem Transport; ein fehlgeschlagener oder abgebrochener Scan startet keinen Druck | Vollständigen Ablauf mit Papierausgabe bestätigen |
| Profile | Benannte Einstellungen, Prüfung vor Verwendung, Export und Import; Anwenden startet keine Geräteaktion | Verfügbare Scannerwerte am Gerät bestätigen |
| Sicherung | Ausgewählte Originale, Text und Metadaten; tatsächlicher ZIP-Download und Wiederherstellung über HTTPS; Fehler und Abbruch übernehmen keine Teilmenge | Kein Gerät für gespeicherte Dateien nötig |
| Historie | Lokalisierte Zeitangaben, sichtbare offene Vorgänge und jüngste 50 Einträge; dauerhafte Wiedererkennung auch nach dem Löschen von Eingabedateien | Tatsächlichen Abbruch und die Erholung des Geräts bestätigen |
| Wartung | Hinweise zum offiziellen Modellhandbuch; dieser Bereich sendet keine Gerätebefehle | Unterstützte Befehle und Auswirkungen gesondert prüfen, bevor sie aktiviert werden |
| Speicherkarte | Echter FAT12/16/32-Lesezugriff auf isolierte Abbilder, Unicode-Ordner, Seitenwechsel, Medienwechsel und geschützter JPEG-/PNG-/PDF-Import mit Vorschau und Download | Den unter USB `04f9:01cf` bereitgestellten Datenträger einer echten Karte erkennen und lesend prüfen; nicht jeder USB-Datenträger wird angenommen |
| Scan-Taste | Nativer USB-Statusempfänger, vier explizite Profilzuordnungen, dauerhafte Aufträge und Duplikatschutz; konstruierte Meldungen, echte Dokument-/OCR-Verarbeitung und eine Aufnahme mit isoliertem virtuellem SANE-Scanner geprüft | Statusmeldungen und gegebenenfalls notwendige Gerätevorbereitung am DCP-150C, tatsächliche Tastendrücke, Wiederverbindung und Halten/Wiederholen bestätigen |
| Weitergabe als Netzwerkscanner | Als spätere Prüfung erfasst; Browserzugriff ist bereits vorgesehen | Zuerst echte Scanneraufnahme abnehmen, danach Nutzung durch andere Scannerprogramme prüfen |

Die [Softwareprüfungen](control-center-validation.json) nennen ihren jeweiligen Umfang. Automatisierte Scannerprüfungen verwenden ausdrücklich den isolierten SANE-Testtreiber. Sie liefern keinen Nachweis einer Aufnahme am physischen DCP-150C.

Ein Geräteversuch braucht einen eingeschalteten, angeschlossenen Drucker und die ausdrückliche Freigabe für den konkreten Druck-, Scan- oder Reinigungsvorgang. Installation, Browser-Neuladen und die hier dokumentierten Softwareprüfungen lösen solche Vorgänge nicht aus.
