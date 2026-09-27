package edubotics;

import java.io.IOException;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Der eine Client: Rahmen, Schlüssel, Aufruf-Bremse, Antwort-Auswertung.
 *
 * GENERATED FILE — do not edit by hand. Rendered from the table in
 * physical_ai_server/workflow/robot_api.py; test_robot_api_generated.py
 * refuses a drift and documents how to regenerate.
 */
public final class RpcClient {

    static final double MAX_CALLS_PER_S = 200.0;
    static final int BURST = 50;
    static final double PERCEPTION_MAX_PER_S = 20.0;
    static final int PERCEPTION_BURST = 5;
    static final int MAX_FRAME_BYTES = 65536;

    static final String SOCKET_ENV = "CODE_RPC_SOCKET";
    static final String TOKEN_ENV = "CODE_RUN_TOKEN";

    static final String NO_CONNECTION_DE =
        "Keine Verbindung zum Roboter — das Programm muss über Roboter Studio gestartet werden.";
    static final String CONNECTION_LOST_DE = "Die Verbindung zum Roboter ist abgebrochen.";
    static final String FRAME_TOO_BIG_DE = "Der Aufruf ist zu groß für den Roboter.";
    static final String BAD_REPLY_DE = "Der Roboter hat unverständlich geantwortet.";

    /** Der Roboter hat einen Aufruf abgelehnt; getMessage() ist die deutsche Meldung. */
    public static final class RobotError extends RuntimeException {
        private static final long serialVersionUID = 1L;

        RobotError(String message) {
            super(message);
        }
    }

    /** A token bucket that SLEEPS until a token is available (never drops). */
    private static final class Bucket {
        private final double rate;
        private final double burst;
        private double tokens;
        private long lastNanos;

        Bucket(double rate, int burst) {
            this.rate = rate;
            this.burst = burst;
            this.tokens = burst;
            this.lastNanos = System.nanoTime();
        }

        synchronized void take() {
            while (true) {
                long now = System.nanoTime();
                tokens = Math.min(burst, tokens + (now - lastNanos) / 1e9 * rate);
                lastNanos = now;
                if (tokens >= 1.0) {
                    tokens -= 1.0;
                    return;
                }
                long waitMillis = (long) Math.ceil((1.0 - tokens) / rate * 1000.0);
                try {
                    Thread.sleep(Math.max(1L, waitMillis));
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
        }
    }

    private static final Object LOCK = new Object();
    private static final Bucket CALLS = new Bucket(MAX_CALLS_PER_S, BURST);
    private static final Bucket PERCEPTION = new Bucket(PERCEPTION_MAX_PER_S, PERCEPTION_BURST);
    private static SocketChannel channel;
    private static String socketPath;
    private static String token;
    private static long nextId;

    private RpcClient() {
    }

    /** Where to connect and how to greet; the launcher sets it, else the two environment variables. */
    static void configure(String path, String runToken) {
        synchronized (LOCK) {
            socketPath = path;
            token = runToken;
        }
    }

    private static void ensureConnected() {
        if (channel != null) {
            return;
        }
        if (socketPath == null || token == null) {
            socketPath = System.getenv(SOCKET_ENV);
            token = System.getenv(TOKEN_ENV);
        }
        if (socketPath == null || token == null) {
            throw new RobotError(NO_CONNECTION_DE);
        }
        SocketChannel ch;
        try {
            ch = SocketChannel.open(StandardProtocolFamily.UNIX);
            ch.connect(UnixDomainSocketAddress.of(socketPath));
        } catch (IOException | UnsupportedOperationException e) {
            throw new RobotError(NO_CONNECTION_DE);
        }
        channel = ch;
        call("__hello", new Object[] {token}, "call");
    }

    private static void rateFloor(String kind) {
        CALLS.take();
        if ("perception".equals(kind)) {
            PERCEPTION.take();
        }
    }

    static Object call(String method, Object[] args, String kind) {
        Map<String, Object> reply;
        synchronized (LOCK) {
            ensureConnected();
            rateFloor(kind);
            nextId++;
            Map<String, Object> request = new LinkedHashMap<>();
            request.put("id", nextId);
            request.put("m", method);
            request.put("a", Arrays.asList(args));
            byte[] frame = encodeFrame(request);
            try {
                writeFully(ByteBuffer.wrap(frame));
                reply = EduJson.asObject(readFrame());
            } catch (IOException e) {
                close();
                throw new RobotError(CONNECTION_LOST_DE);
            }
        }
        if (Boolean.TRUE.equals(reply.get("ok"))) {
            return reply.get("r");
        }
        Object e = reply.get("e");
        throw new RobotError(e == null ? BAD_REPLY_DE : String.valueOf(e));
    }

    /** u32 big-endian length + the JSON object as UTF-8 (umlauts raw, compact). */
    static byte[] encodeFrame(Object obj) {
        byte[] body = EduJson.encode(obj).getBytes(StandardCharsets.UTF_8);
        if (body.length > MAX_FRAME_BYTES) {
            throw new RobotError(FRAME_TOO_BIG_DE);
        }
        ByteBuffer out = ByteBuffer.allocate(4 + body.length);
        out.putInt(body.length);
        out.put(body);
        return out.array();
    }

    private static void writeFully(ByteBuffer buf) throws IOException {
        while (buf.hasRemaining()) {
            channel.write(buf);
        }
    }

    private static ByteBuffer readExact(int n) throws IOException {
        ByteBuffer buf = ByteBuffer.allocate(n);
        while (buf.hasRemaining()) {
            if (channel.read(buf) < 0) {
                throw new IOException("closed");
            }
        }
        buf.flip();
        return buf;
    }

    private static Object readFrame() throws IOException {
        int length = readExact(4).getInt();
        if (length < 0 || length > MAX_FRAME_BYTES) {
            throw new RobotError(BAD_REPLY_DE);
        }
        ByteBuffer body = readExact(length);
        String text = StandardCharsets.UTF_8.decode(body).toString();
        Object reply;
        try {
            reply = EduJson.decode(text);
        } catch (IllegalArgumentException e) {
            throw new RobotError(BAD_REPLY_DE);
        }
        if (!(reply instanceof Map)) {
            throw new RobotError(BAD_REPLY_DE);
        }
        return reply;
    }

    static void close() {
        SocketChannel ch;
        synchronized (LOCK) {
            ch = channel;
            channel = null;
        }
        if (ch != null) {
            try {
                ch.close();
            } catch (IOException e) {
                // nothing left to do with a socket that will not close
            }
        }
    }

    static boolean asBoolean(Object r) {
        return Boolean.TRUE.equals(r);
    }

    static long asLong(Object r) {
        if (r instanceof Number) {
            return ((Number) r).longValue();
        }
        throw new RobotError(BAD_REPLY_DE);
    }

    static int asInt(Object r) {
        return (int) asLong(r);
    }

    static String asString(Object r) {
        if (r instanceof String) {
            return (String) r;
        }
        throw new RobotError(BAD_REPLY_DE);
    }

    // ── zeige(): bounded, JSON-safe renderings of a shown value ─────────────

    static final int SHOWN_MAX_ITEMS = 50;
    static final int SHOWN_MAX_CHARS = 1000;

    static Object shownDouble(double v) {
        if (Double.isNaN(v) || Double.isInfinite(v)) {
            return String.valueOf(v);
        }
        return v;
    }

    static String shownText(String s) {
        if (s == null) {
            return null;
        }
        return s.length() > SHOWN_MAX_CHARS ? s.substring(0, SHOWN_MAX_CHARS) : s;
    }

    static List<Object> shownDoubles(double[] a) {
        if (a == null) {
            return null;
        }
        int n = Math.min(a.length, SHOWN_MAX_ITEMS);
        List<Object> out = new java.util.ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            out.add(shownDouble(a[i]));
        }
        return out;
    }

    static List<Object> shownInts(int[] a) {
        if (a == null) {
            return null;
        }
        int n = Math.min(a.length, SHOWN_MAX_ITEMS);
        List<Object> out = new java.util.ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            out.add(a[i]);
        }
        return out;
    }

    static Object shownObject(Object o) {
        if (o == null || o instanceof Boolean || o instanceof Integer || o instanceof Long
                || o instanceof Short || o instanceof Byte) {
            return o;
        }
        if (o instanceof Number) {
            return shownDouble(((Number) o).doubleValue());
        }
        if (o instanceof double[]) {
            return shownDoubles((double[]) o);
        }
        if (o instanceof int[]) {
            return shownInts((int[]) o);
        }
        if (o instanceof Iterable) {
            List<Object> out = new java.util.ArrayList<>();
            for (Object item : (Iterable<?>) o) {
                if (out.size() >= SHOWN_MAX_ITEMS) {
                    break;
                }
                out.add(shownText(String.valueOf(item)));
            }
            return out;
        }
        return shownText(String.valueOf(o));
    }

    static double[] asPoint(Object r) {
        if (r == null) {
            return null;
        }
        List<Object> items = EduJson.asArray(r);
        if (items.size() != 3) {
            throw new RobotError(BAD_REPLY_DE);
        }
        double[] out = new double[3];
        for (int i = 0; i < 3; i++) {
            Object v = items.get(i);
            if (!(v instanceof Number)) {
                throw new RobotError(BAD_REPLY_DE);
            }
            out[i] = ((Number) v).doubleValue();
        }
        return out;
    }
}
