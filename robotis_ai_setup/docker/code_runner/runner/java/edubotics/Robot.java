package edubotics;

/**
 * Roboter-API für EduBotics-Programme (Java).
 *
 * <pre>
 * Robot.home();
 * Robot.moveTo(new double[] {0.15, 0.0, 0.05});
 * Robot.Greifziel ziel = Robot.find("wuerfel");
 * if (ziel != null) Robot.moveAbove(ziel);
 * </pre>
 *
 * Jede Methode schickt einen Aufruf an den Roboter und wartet auf die Antwort.
 * Lehnt der Roboter etwas ab, gibt es eine RpcClient.RobotError mit deutscher
 * Meldung.
 *
 * GENERATED FILE — do not edit by hand. Rendered from the table in
 * physical_ai_server/workflow/robot_api.py; test_robot_api_generated.py
 * refuses a drift and documents how to regenerate.
 */
public final class Robot {

    private Robot() {
    }

    /** Ein gefundenes Objekt (von Robot.find) — ein Handle für die Greif-Schritte. */
    public static final class Greifziel {
        private final long handle;

        Greifziel(long handle) {
            this.handle = handle;
        }

        long handle() {
            return handle;
        }

        @Override
        public String toString() {
            return "Greifziel(" + handle + ")";
        }

        @Override
        public boolean equals(Object other) {
            return other instanceof Greifziel && ((Greifziel) other).handle == handle;
        }

        @Override
        public int hashCode() {
            return Long.hashCode(handle);
        }
    }

    private static Greifziel toGreifziel(Object r) {
        if (r == null) {
            return null;
        }
        return new Greifziel(RpcClient.asLong(EduJson.asObject(r).get("h")));
    }

    /** Fährt den Arm in die Grundstellung; der Greifer bleibt, wie er ist. */
    public static void home() {
        RpcClient.call("home", new Object[] {}, "call");
    }

    /** Öffnet den Greifer. */
    public static void openGripper() {
        RpcClient.call("open_gripper", new Object[] {}, "call");
    }

    /** Schließt den Greifer. */
    public static void closeGripper() {
        RpcClient.call("close_gripper", new Object[] {}, "call");
    }

    /** Fährt über das Ziel: ein Punkt [x, y, z] in Metern oder der Name eines gemerkten Ziels. */
    public static void moveTo(double[] target) {
        RpcClient.call("move_to", new Object[] {target}, "call");
    }

    /** Fährt über das Ziel: ein Punkt [x, y, z] in Metern oder der Name eines gemerkten Ziels. */
    public static void moveTo(String target) {
        RpcClient.call("move_to", new Object[] {target}, "call");
    }

    /** Nimmt an dieser Stelle etwas auf: hinfahren, absenken, Greifer schließen, anheben. */
    public static void pickup(double[] target) {
        RpcClient.call("pickup", new Object[] {target}, "call");
    }

    /** Nimmt an dieser Stelle etwas auf: hinfahren, absenken, Greifer schließen, anheben. */
    public static void pickup(String target) {
        RpcClient.call("pickup", new Object[] {target}, "call");
    }

    /** Legt das Gehaltene über dem Ziel ab und öffnet den Greifer. */
    public static void dropAt(double[] target) {
        RpcClient.call("drop_at", new Object[] {target}, "call");
    }

    /** Legt das Gehaltene über dem Ziel ab und öffnet den Greifer. */
    public static void dropAt(String target) {
        RpcClient.call("drop_at", new Object[] {target}, "call");
    }

    /** Wartet so viele Sekunden (höchstens 300). */
    public static void waitSeconds(double seconds) {
        RpcClient.call("wait", new Object[] {seconds}, "call");
    }

    /** Spielt eine aufgenommene Bewegung ab; speed 1.0 ist die Geschwindigkeit der Aufnahme, höchstens 3.0. */
    public static void replay(String name, double speed) {
        RpcClient.call("replay", new Object[] {name, speed}, "call");
    }

    /** Spielt eine aufgenommene Bewegung ab; speed 1.0 ist die Geschwindigkeit der Aufnahme, höchstens 3.0. */
    public static void replay(String name) {
        RpcClient.call("replay", new Object[] {name, 1.0}, "call");
    }

    /** Fährt in Anfahrhöhe über das gefundene Objekt. */
    public static void moveAbove(Greifziel ziel) {
        RpcClient.call("move_above", new Object[] {ziel.handle()}, "call");
    }

    /** Senkt den geöffneten Greifer auf das gefundene Objekt ab. */
    public static void descendTo(Greifziel ziel) {
        RpcClient.call("descend_to", new Object[] {ziel.handle()}, "call");
    }

    /** Schließt den Greifer um das gefundene Objekt. */
    public static void closeOnObject(Greifziel ziel) {
        RpcClient.call("close_on_object", new Object[] {ziel.handle()}, "call");
    }

    /** Hebt den Greifer gerade nach oben an, so weit der Arm kann. */
    public static void lift() {
        RpcClient.call("lift", new Object[] {}, "call");
    }

    /** Greift das nächste sichtbare Objekt dieses Typs: finden, anfahren, schließen, prüfen. */
    public static void grasp(String obj) {
        RpcClient.call("grasp", new Object[] {obj}, "call");
    }

    /** Greift das nächste sichtbare Objekt dieses Typs: finden, anfahren, schließen, prüfen. */
    public static void grasp(Greifobjekt obj) {
        RpcClient.call("grasp", new Object[] {obj.name()}, "call");
    }

    /** Merkt dieses Objekt als erledigt, damit es nicht noch einmal gefunden wird. */
    public static void markDone(Greifziel ziel) {
        RpcClient.call("mark_done", new Object[] {ziel.handle()}, "call");
    }

    /** Merkt den Punkt [x, y, z] in Metern unter diesem Namen als Ziel für move_to und drop_at. */
    public static void pin(String name, double x, double y, double z) {
        RpcClient.call("pin", new Object[] {name, x, y, z}, "call");
    }

    /** Merkt die Stelle, an der der Greifer gerade steht, unter diesem Namen als Ziel. */
    public static void pinCurrent(String name) {
        RpcClient.call("pin_current", new Object[] {name}, "call");
    }

    /** Schreibt eine Zeile ins Protokoll — Zahlen und Texte, höchstens 2000 Zeichen. */
    public static void log(Object message) {
        RpcClient.call("log", new Object[] {message}, "call");
    }

    /** Spielt einen kurzen Signalton ab — hörbar im Browser. */
    public static void beep() {
        RpcClient.call("beep", new Object[] {}, "call");
    }

    /** Liest den Text mit deutscher Stimme vor (höchstens 240 Zeichen). */
    public static void speak(Object text) {
        RpcClient.call("speak", new Object[] {text}, "call");
    }

    /** Spielt einen Ton mit dieser Frequenz in Hertz für so viele Sekunden (höchstens 5). */
    public static void tone(double freq, double seconds) {
        RpcClient.call("tone", new Object[] {freq, seconds}, "call");
    }

    /** Spielt einen Ton mit dieser Frequenz in Hertz für so viele Sekunden (höchstens 5). */
    public static void tone(double freq) {
        RpcClient.call("tone", new Object[] {freq, 0.25}, "call");
    }

    /** Spielt einen Ton mit dieser Frequenz in Hertz für so viele Sekunden (höchstens 5). */
    public static void tone() {
        RpcClient.call("tone", new Object[] {880.0, 0.25}, "call");
    }

    /** Zeigt eine Meldung auf dem Bildschirm; level ist info, success, warning oder error, seconds höchstens 15. */
    public static void toast(Object text, String level, int seconds) {
        RpcClient.call("toast", new Object[] {text, level, seconds}, "call");
    }

    /** Zeigt eine Meldung auf dem Bildschirm; level ist info, success, warning oder error, seconds höchstens 15. */
    public static void toast(Object text, String level) {
        RpcClient.call("toast", new Object[] {text, level, 3}, "call");
    }

    /** Zeigt eine Meldung auf dem Bildschirm; level ist info, success, warning oder error, seconds höchstens 15. */
    public static void toast(Object text) {
        RpcClient.call("toast", new Object[] {text, "info", 3}, "call");
    }

    /** Setzt den Zähler mit diesem Namen auf 0. */
    public static void counterReset(String name) {
        RpcClient.call("counter_reset", new Object[] {name}, "call");
    }

    /** Erhöht den Zähler mit diesem Namen um 1. */
    public static void counterAdd(String name) {
        RpcClient.call("counter_add", new Object[] {name}, "call");
    }

    /** Gibt den Namen eines gemerkten Ziels für move_to und drop_at zurück; ein unbekannter Name wird abgelehnt. */
    public static String ziel(String name) {
        Object r = RpcClient.call("ziel", new Object[] {name}, "call");
        return RpcClient.asString(r);
    }

    /** Prüft, ob gerade ein noch nicht erledigtes Objekt dieses Typs zu sehen ist. */
    public static boolean sees(String obj) {
        Object r = RpcClient.call("sees", new Object[] {obj}, "perception");
        return RpcClient.asBoolean(r);
    }

    /** Prüft, ob gerade ein noch nicht erledigtes Objekt dieses Typs zu sehen ist. */
    public static boolean sees(Greifobjekt obj) {
        Object r = RpcClient.call("sees", new Object[] {obj.name()}, "perception");
        return RpcClient.asBoolean(r);
    }

    /** Zählt die sichtbaren, noch nicht erledigten Objekte dieses Typs. */
    public static int count(String obj) {
        Object r = RpcClient.call("count", new Object[] {obj}, "perception");
        return RpcClient.asInt(r);
    }

    /** Zählt die sichtbaren, noch nicht erledigten Objekte dieses Typs. */
    public static int count(Greifobjekt obj) {
        Object r = RpcClient.call("count", new Object[] {obj.name()}, "perception");
        return RpcClient.asInt(r);
    }

    /** Wartet, bis ein Objekt dieses Typs zu sehen ist; False, wenn es nach timeout Sekunden nicht da ist. */
    public static boolean waitUntilSeen(String obj, double timeout) {
        Object r = RpcClient.call("wait_until_seen", new Object[] {obj, timeout}, "perception");
        return RpcClient.asBoolean(r);
    }

    /** Wartet, bis ein Objekt dieses Typs zu sehen ist; False, wenn es nach timeout Sekunden nicht da ist. */
    public static boolean waitUntilSeen(Greifobjekt obj, double timeout) {
        Object r = RpcClient.call("wait_until_seen", new Object[] {obj.name(), timeout}, "perception");
        return RpcClient.asBoolean(r);
    }

    /** Wartet, bis ein Objekt dieses Typs zu sehen ist; False, wenn es nach timeout Sekunden nicht da ist. */
    public static boolean waitUntilSeen(String obj) {
        Object r = RpcClient.call("wait_until_seen", new Object[] {obj, 10.0}, "perception");
        return RpcClient.asBoolean(r);
    }

    /** Wartet, bis ein Objekt dieses Typs zu sehen ist; False, wenn es nach timeout Sekunden nicht da ist. */
    public static boolean waitUntilSeen(Greifobjekt obj) {
        Object r = RpcClient.call("wait_until_seen", new Object[] {obj.name(), 10.0}, "perception");
        return RpcClient.asBoolean(r);
    }

    /** Wartet, bis der Greifer etwas hält; False, wenn er nach timeout Sekunden noch leer ist. */
    public static boolean waitUntilHeld(double timeout) {
        Object r = RpcClient.call("wait_until_held", new Object[] {timeout}, "call");
        return RpcClient.asBoolean(r);
    }

    /** Wartet, bis der Greifer etwas hält; False, wenn er nach timeout Sekunden noch leer ist. */
    public static boolean waitUntilHeld() {
        Object r = RpcClient.call("wait_until_held", new Object[] {10.0}, "call");
        return RpcClient.asBoolean(r);
    }

    /** Findet das nächste greifbare Objekt dieses Typs und gibt ein Greifziel zurück, sonst None. */
    public static Greifziel find(String obj) {
        Object r = RpcClient.call("find", new Object[] {obj}, "perception");
        return toGreifziel(r);
    }

    /** Findet das nächste greifbare Objekt dieses Typs und gibt ein Greifziel zurück, sonst None. */
    public static Greifziel find(Greifobjekt obj) {
        Object r = RpcClient.call("find", new Object[] {obj.name()}, "perception");
        return toGreifziel(r);
    }

    /** Gibt die Position [x, y, z] des Greifziels in Metern zurück. */
    public static double[] objectPosition(Greifziel ziel) {
        Object r = RpcClient.call("object_position", new Object[] {ziel.handle()}, "perception");
        return RpcClient.asPoint(r);
    }

    /** Prüft, ob der Greifer etwas hält. */
    public static boolean isHolding() {
        Object r = RpcClient.call("is_holding", new Object[] {}, "call");
        return RpcClient.asBoolean(r);
    }

    /** Gibt den Wert des Zählers zurück; 0, wenn er noch nie gesetzt wurde. */
    public static int counterGet(String name) {
        Object r = RpcClient.call("counter_get", new Object[] {name}, "call");
        return RpcClient.asInt(r);
    }

    /** Zeigt einen Wert unter diesem Namen im Variablen-Bereich an — Zahlen, Texte, Listen; höchstens 40 Zeichen Name. */
    public static void zeige(String name, int wert) {
        RpcClient.call("zeige", new Object[] {name, wert}, "call");
    }

    /** Zeigt einen Wert unter diesem Namen im Variablen-Bereich an — Zahlen, Texte, Listen; höchstens 40 Zeichen Name. */
    public static void zeige(String name, long wert) {
        RpcClient.call("zeige", new Object[] {name, wert}, "call");
    }

    /** Zeigt einen Wert unter diesem Namen im Variablen-Bereich an — Zahlen, Texte, Listen; höchstens 40 Zeichen Name. */
    public static void zeige(String name, double wert) {
        RpcClient.call("zeige", new Object[] {name, RpcClient.shownDouble(wert)}, "call");
    }

    /** Zeigt einen Wert unter diesem Namen im Variablen-Bereich an — Zahlen, Texte, Listen; höchstens 40 Zeichen Name. */
    public static void zeige(String name, boolean wert) {
        RpcClient.call("zeige", new Object[] {name, wert}, "call");
    }

    /** Zeigt einen Wert unter diesem Namen im Variablen-Bereich an — Zahlen, Texte, Listen; höchstens 40 Zeichen Name. */
    public static void zeige(String name, char wert) {
        RpcClient.call("zeige", new Object[] {name, String.valueOf(wert)}, "call");
    }

    /** Zeigt einen Wert unter diesem Namen im Variablen-Bereich an — Zahlen, Texte, Listen; höchstens 40 Zeichen Name. */
    public static void zeige(String name, Object wert) {
        RpcClient.call("zeige", new Object[] {name, RpcClient.shownObject(wert)}, "call");
    }
}
