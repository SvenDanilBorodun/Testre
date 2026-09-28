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
        // EVERY array type is a JSON list, never the JVM's "[J@1b6d3586"
        // (review round 3, nb6); nested arrays nest, and past the depth "…".
        check(EduJson.encode(RpcClient.shownObject(new long[] {1L, 2L})).equals("[1,2]"), "zeige long[]");
        check(EduJson.encode(RpcClient.shownObject(new String[] {"a", "b"})).equals("[\"a\",\"b\"]"),
              "zeige String[]");
        check(EduJson.encode(RpcClient.shownObject(new boolean[] {true, false})).equals("[true,false]"),
              "zeige boolean[]");
        check(EduJson.encode(RpcClient.shownObject(new char[] {'a', 'b'})).equals("[\"a\",\"b\"]"),
              "zeige char[]");
        check(EduJson.encode(RpcClient.shownObject(new float[] {1.5f})).equals("[1.5]"), "zeige float[]");
        check(EduJson.encode(RpcClient.shownObject(new short[] {3})).equals("[3]"), "zeige short[]");
        check(EduJson.encode(RpcClient.shownObject(new byte[] {4})).equals("[4]"), "zeige byte[]");
        check(EduJson.encode(RpcClient.shownObject(new Object[] {1, "x", null})).equals("[1,\"x\",null]"),
              "zeige Object[]");
        check(EduJson.encode(RpcClient.shownObject(new int[][] {{1, 2}, {3}})).equals("[[1,2],[3]]"),
              "zeige int[][]");
        check(EduJson.encode(RpcClient.shownObject(new int[][][][] {{{{1}}}})).equals("[[[\"…\"]]]"),
              "zeige cuts past the depth");
        check(EduJson.encode(RpcClient.shownObject(List.of(1, "a"))).equals("[1,\"a\"]"), "zeige list");
        int[] viele = new int[80];
        String cut = EduJson.encode(RpcClient.shownObject(viele));
        check(cut.endsWith(",\"…\"]") && cut.split(",").length == RpcClient.SHOWN_MAX_ITEMS + 1,
              "zeige caps an array at SHOWN_MAX_ITEMS");
        String langArray = EduJson.encode(RpcClient.shownObject(new long[] {7L}));
        check(!langArray.contains("@"), "zeige never shows an array's identity");
        // A list's items share ONE character budget: sixty 1000-character
        // texts stay a small frame, cut with "…".
        List<String> lang = new java.util.ArrayList<>();
        for (int i = 0; i < 60; i++) {
            lang.add("ü".repeat(1000));
        }
        String bounded = EduJson.encode(RpcClient.shownObject(lang));
        check(bounded.length() < 2 * RpcClient.SHOWN_BUDGET_CHARS && bounded.endsWith("\"…\"]"),
              "zeige list shares one budget");
        // A value the student's own code cannot render never reaches the
        // program as an exception: "<?>", like the Python stub (review round
        // 4, nc1) — a failing toString(), a failing iterator, a
        // ConcurrentModificationException.
        Object boese = new Object() {
            @Override
            public String toString() {
                throw new IllegalStateException("toString kaputt");
            }
        };
        check("<?>".equals(RpcClient.shownObject(boese)), "zeige a failing toString");
        List<Object> mitBoese = new java.util.ArrayList<>();
        mitBoese.add(1);
        mitBoese.add(boese);
        check(EduJson.encode(RpcClient.shownObject(mitBoese)).equals("[1,\"<?>\"]"), "zeige a list with one");
        Iterable<Integer> kaputt = () -> new java.util.Iterator<Integer>() {
            @Override
            public boolean hasNext() {
                return true;
            }

            @Override
            public Integer next() {
                throw new java.util.ConcurrentModificationException();
            }
        };
        check("<?>".equals(RpcClient.shownObject(kaputt)), "zeige a failing iterator");
        boolean refusedBoese = false;
        try {
            Robot.zeige("x", boese);
        } catch (RpcClient.RobotError e) {
            refusedBoese = true;
        }
        check(refusedBoese, "zeige of a failing toString is refused for the SOCKET only");
        // A Path is its text, not the Iterable of its parts; a number too big
        // for a double its own text, never "Infinity" (nc1).
        check("/tmp/ordner/datei.txt".equals(RpcClient.shownObject(java.nio.file.Path.of("/tmp/ordner/datei.txt"))),
              "zeige Path");
        String gross = "1" + "0".repeat(400);
        check(gross.equals(RpcClient.shownObject(new java.math.BigInteger(gross))), "zeige huge BigInteger");
        check("1E+400".equals(RpcClient.shownObject(new java.math.BigDecimal("1e400"))), "zeige huge BigDecimal");
        check(Long.valueOf(12L).equals(RpcClient.shownObject(java.math.BigInteger.valueOf(12))), "zeige small BigInteger");
        check(Double.valueOf(2.5).equals(RpcClient.shownObject(new java.math.BigDecimal("2.5"))), "zeige BigDecimal");
        // A whole number past ±2^53 is a double (JavaScript would round it
        // silently), within it exact — the Python stub's convention (nc2).
        long grenze = 1L << 53;
        check(Long.valueOf(grenze).equals(RpcClient.shownLong(grenze)), "zeige long at 2^53");
        check(Double.valueOf((double) (grenze + 1)).equals(RpcClient.shownLong(grenze + 1)), "zeige long past 2^53");
        check(Double.valueOf((double) Long.MIN_VALUE).equals(RpcClient.shownObject(Long.MIN_VALUE)), "zeige Long past -2^53");
        // Review round 5 (nd2): ANY Throwable of the student's own code is
        // "<?>" — an AssertionError from toString() reached the program — and
        // only the JVM's own failures go on.
        Object assertBoese = new Object() {
            @Override
            public String toString() {
                throw new AssertionError("toString kaputt");
            }
        };
        check("<?>".equals(RpcClient.shownObject(assertBoese)), "zeige an AssertionError in toString");
        Object eigenerFehler = new Object() {
            @Override
            public String toString() {
                throw new ExceptionInInitializerError("eigener Fehler");
            }
        };
        check("<?>".equals(RpcClient.shownObject(eigenerFehler)), "zeige any other Error");
        Object speicher = new Object() {
            @Override
            public String toString() {
                throw new OutOfMemoryError("simuliert");
            }
        };
        boolean vmFehler = false;
        try {
            RpcClient.shownObject(speicher);
        } catch (OutOfMemoryError e) {
            vmFehler = true;
        }
        check(vmFehler, "zeige lets the JVM's own failure go on");
        // A Map is its entries, never its toString(); huge values are bounded
        // BEFORE any text is built (nd2).
        check(EduJson.encode(RpcClient.shownObject(Map.of("k", 1))).equals("{\"k\":1}"), "zeige Map");
        Map<Object, Object> ohneText = new java.util.LinkedHashMap<Object, Object>() {
            private static final long serialVersionUID = 1L;

            @Override
            public String toString() {
                throw new IllegalStateException("nie aufgerufen");
            }
        };
        ohneText.put(1, "eins");
        check(EduJson.encode(RpcClient.shownObject(ohneText)).equals("{\"1\":\"eins\"}"),
              "zeige a Map through its entries");
        Map<Integer, Integer> riesig = new java.util.HashMap<>();
        for (int i = 0; i < 1_000_000; i++) {
            riesig.put(i, i);
        }
        long t0 = System.nanoTime();
        Object riesigGezeigt = RpcClient.shownObject(riesig);
        long mapMs = (System.nanoTime() - t0) / 1_000_000;
        check(riesigGezeigt instanceof Map && ((Map<?, ?>) riesigGezeigt).size() == RpcClient.SHOWN_MAX_ITEMS + 1
              && ((Map<?, ?>) riesigGezeigt).containsKey("…"), "zeige caps a Map and says so");
        check(mapMs < 500, "zeige a Map of a million entries is cheap: " + mapMs + " ms");
        java.math.BigInteger sehrGross = java.math.BigInteger.ONE.shiftLeft(1 << 22);
        t0 = System.nanoTime();
        Object grossGezeigt = RpcClient.shownObject(sehrGross);
        long bigMs = (System.nanoTime() - t0) / 1_000_000;
        check(RpcClient.SHOWN_TOO_BIG.equals(grossGezeigt), "zeige a BigInteger of millions of bits");
        check(bigMs < 200, "zeige a BigInteger of millions of bits is cheap: " + bigMs + " ms");
        check(RpcClient.SHOWN_TOO_BIG.equals(RpcClient.shownObject(new java.math.BigDecimal(sehrGross, 3))),
              "zeige a BigDecimal of millions of bits");
        String tausend = "1" + "0".repeat(999);
        check(tausend.equals(RpcClient.shownObject(new java.math.BigInteger(tausend))),
              "zeige a thousand-digit BigInteger is still its text");
        check(RpcClient.SHOWN_TOO_BIG.equals(RpcClient.shownObject(java.math.BigInteger.TEN.pow(1001))),
              "zeige past the text's thousand characters");
        StringBuilder langerText = new StringBuilder();
        for (int i = 0; i < 1_000_000; i++) {
            langerText.append('x');
        }
        Object textGezeigt = RpcClient.shownObject(langerText);
        check(textGezeigt instanceof String && ((String) textGezeigt).length() == RpcClient.SHOWN_MAX_CHARS,
              "zeige a CharSequence cut before it becomes text");
        check(RpcClient.asPoint(null) == null, "null point");
        check(RpcClient.asPoint(List.of(1.0, 2L, 3.5))[1] == 2.0, "point decode");

        System.out.println("JAVA SMOKE OK");
    }
}
