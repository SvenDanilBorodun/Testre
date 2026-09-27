package edubotics;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Build-time smoke: round-trips a frame through EduJson / RpcClient.encodeFrame
 * and exercises the Robot / Greifobjekt surface against a socket path that
 * does not exist (a German RobotError is the expected outcome). Hand-written;
 * run by the javac-smoke gate and by the image build.
 */
public final class SmokeMain {

    private SmokeMain() {
    }

    private static void check(boolean ok, String what) {
        if (!ok) {
            throw new IllegalStateException("smoke failed: " + what);
        }
    }

    public static void main(String[] args) {
        // 1. A frame: u32 length + compact UTF-8 JSON with the umlaut RAW.
        Map<String, Object> request = new LinkedHashMap<>();
        request.put("id", 1L);
        request.put("m", "move_to");
        request.put("a", List.of(List.of(0.1, -0.2, 0.03), "Ablage ü"));
        byte[] frame = RpcClient.encodeFrame(request);
        int length = ByteBuffer.wrap(frame).getInt();
        check(length == frame.length - 4, "length prefix");
        String body = new String(frame, 4, length, StandardCharsets.UTF_8);
        check(body.contains("ü") && !body.contains("\\u00fc"), "umlaut stays raw");
        check(body.startsWith("{\"id\":1,\"m\":\"move_to\",\"a\":[[0.1,-0.2,0.03],"),
              "compact separators");
        Map<String, Object> back = EduJson.asObject(EduJson.decode(body));
        check("move_to".equals(back.get("m")), "method survives");
        check(Long.valueOf(1L).equals(back.get("id")), "integer survives");
        List<Object> a = EduJson.asArray(back.get("a"));
        check("Ablage ü".equals(a.get(1)), "string survives");
        check(EduJson.asArray(a.get(0)).size() == 3, "point survives");
        check("\"a\\\"b\\n\"".equals(EduJson.encode("a\"b\n")), "escapes");
        check("x\u0001".equals(EduJson.decode("\"x\\u0001\"")), "unicode escape");

        // 2. The Robot surface against a socket that does not exist.
        RpcClient.configure("/nirgends/rpc.sock", "0123456789abcdef0123456789abcdef");
        boolean refused = false;
        try {
            Robot.home();
        } catch (RpcClient.RobotError e) {
            refused = e.getMessage().contains("Roboter");
        }
        check(refused, "home() refused in German without a socket");
        refused = false;
        try {
            new Greifobjekt("Banane", new int[] {30, 31}, 0.040, 0.015);
        } catch (RpcClient.RobotError e) {
            refused = e.getMessage().contains("Roboter");
        }
        check(refused, "Greifobjekt refused in German without a socket");
        refused = false;
        try {
            Robot.find("wuerfel");
        } catch (RpcClient.RobotError e) {
            refused = true;
        }
        check(refused, "find() refused without a socket");
        // zeige(): every value a student may hold COMPILES (null included —
        // one reference overload, dispatched at run time) and renders
        // JSON-safe; without a socket each call is refused.
        int zeigeRefused = 0;
        Object[] shown = {null, "Würfel", new double[] {1.0, Double.NaN}, new int[] {1, 2},
                          List.of(1, "a"), 'c', 3.5f};
        for (Object value : shown) {
            try {
                Robot.zeige("x", value);
            } catch (RpcClient.RobotError e) {
                zeigeRefused++;
            }
        }
        try {
            Robot.zeige("x", null);
        } catch (RpcClient.RobotError e) {
            zeigeRefused++;
        }
        try {
            Robot.zeige("x", 5);
        } catch (RpcClient.RobotError e) {
            zeigeRefused++;
        }
        check(zeigeRefused == shown.length + 2, "zeige refused without a socket");
        check(RpcClient.shownObject(null) == null, "zeige null");
        check("Würfel".equals(RpcClient.shownObject("Würfel")), "zeige text");
        check("c".equals(RpcClient.shownObject('c')), "zeige char");
        check(EduJson.encode(RpcClient.shownObject(new double[] {1.0, Double.NaN}))
                .equals("[1.0,\"NaN\"]"), "zeige double[]");
        check(EduJson.encode(RpcClient.shownObject(new int[] {1, 2})).equals("[1,2]"), "zeige int[]");
        check(RpcClient.asPoint(null) == null, "null point");
        check(RpcClient.asPoint(List.of(1.0, 2L, 3.5))[1] == 2.0, "point decode");

        System.out.println("JAVA SMOKE OK");
    }
}
