package edubotics;

import java.util.Locale;

/**
 * Eigener Objekt-Typ: anlegen, fertig.
 *
 * <pre>
 * Greifobjekt banane = new Greifobjekt("Banane", new int[] {30, 31}, 0.040, 0.015);
 * Robot.grasp(banane);
 * </pre>
 *
 * Der Name ist das Label in Kleinbuchstaben; zwei optionale Zahlen am Ende
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
        this.name = label.toLowerCase(Locale.ROOT);
        RpcClient.call("register_object", new Object[] {this.name, label, tagIds, hoeheM, greiftiefeM, greiferSchliessenRad, anfahrhoeheM}, "perception");
    }

    /** Der Name, unter dem der Roboter diesen Typ kennt. */
    public String name() {
        return name;
    }
}
