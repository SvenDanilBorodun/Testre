package edubotics;

import java.text.Normalizer;
import java.util.Locale;

/**
 * Eigener Objekt-Typ: anlegen, fertig.
 *
 * <pre>
 * Greifobjekt banane = new Greifobjekt("Banane", new int[] {30, 31}, 0.040, 0.015);
 * Robot.grasp(banane);
 * </pre>
 *
 * Der Name kommt aus dem Label: Kleinbuchstaben, ä wird ae, ß wird ss, alles
 * andere _ ("Rote Banane" wird rote_banane). Zwei optionale Zahlen am Ende
 * sind greiferSchliessenRad und anfahrhoeheM. Das Objekt wird beim Anlegen
 * für diesen Lauf beim Roboter angemeldet.
 *
 * GENERATED FILE — do not edit by hand. Rendered from the table in
 * physical_ai_server/workflow/robot_api.py; test_robot_api_generated.py
 * refuses a drift and documents how to regenerate.
 */
public final class Greifobjekt {

    private final String name;

    public Greifobjekt(String label, int[] tagIds, double hoeheM, double greiftiefeM) {
        this(label, tagIds, hoeheM, greiftiefeM, null, null);
    }

    public Greifobjekt(String label, int[] tagIds, double hoeheM, double greiftiefeM,
                       double greiferSchliessenRad) {
        this(label, tagIds, hoeheM, greiftiefeM, Double.valueOf(greiferSchliessenRad), null);
    }

    public Greifobjekt(String label, int[] tagIds, double hoeheM, double greiftiefeM,
                       double greiferSchliessenRad, double anfahrhoeheM) {
        this(label, tagIds, hoeheM, greiftiefeM, Double.valueOf(greiferSchliessenRad),
             Double.valueOf(anfahrhoeheM));
    }

    private Greifobjekt(String label, int[] tagIds, double hoeheM, double greiftiefeM,
                        Double greiferSchliessenRad, Double anfahrhoeheM) {
        this.name = typeNameFor(label);
        RpcClient.call("register_object", new Object[] {this.name, label, tagIds, hoeheM, greiftiefeM, greiferSchliessenRad, anfahrhoeheM}, "perception");
    }

    // robot_api.type_name_for, spelled in Java (SmokeMain asserts the two
    // agree on the same table): NFC, the German letter pairs, lowercase, every
    // run of anything but a-z/0-9 one "_", none at either end, at most
    // TYPE_NAME_MAX_CHARS characters.
    static final int TYPE_NAME_MAX_CHARS = 24;
    static final String TYPE_NAME_FALLBACK = "objekt";
    private static final String[][] GERMAN_PAIRS = {{"ä", "ae"}, {"ö", "oe"}, {"ü", "ue"}, {"Ä", "Ae"}, {"Ö", "Oe"}, {"Ü", "Ue"}, {"ß", "ss"}, {"ẞ", "SS"}};

    static String typeNameFor(String label) {
        String text = Normalizer.normalize(String.valueOf(label), Normalizer.Form.NFC);
        for (String[] pair : GERMAN_PAIRS) {
            text = text.replace(pair[0], pair[1]);
        }
        text = stripUnderscores(text.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9]+", "_"));
        if (text.length() > TYPE_NAME_MAX_CHARS) {
            text = stripUnderscores(text.substring(0, TYPE_NAME_MAX_CHARS));
        }
        return text.isEmpty() ? TYPE_NAME_FALLBACK : text;
    }

    private static String stripUnderscores(String text) {
        int start = 0;
        int end = text.length();
        while (start < end && text.charAt(start) == '_') {
            start++;
        }
        while (end > start && text.charAt(end - 1) == '_') {
            end--;
        }
        return text.substring(start, end);
    }

    /** Der Name, unter dem der Roboter diesen Typ kennt. */
    public String name() {
        return name;
    }
}
