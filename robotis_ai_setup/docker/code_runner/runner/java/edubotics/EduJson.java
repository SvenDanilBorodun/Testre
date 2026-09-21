package edubotics;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal JSON codec, Java SE only: objects, arrays, strings, numbers,
 * booleans, null. Hand-written (not generated) — the wire shape it encodes is
 * the one {@code RpcClient} frames, umlauts raw (never {@code ü}), no
 * whitespace, the same bytes the server's {@code json.dumps(ensure_ascii=False,
 * separators=(',', ':'))} produces.
 */
final class EduJson {

    private EduJson() {
    }

    // ── encode ────────────────────────────────────────────────────────────

    static String encode(Object value) {
        StringBuilder sb = new StringBuilder();
        write(sb, value);
        return sb.toString();
    }

    private static void write(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String) {
            writeString(sb, (String) value);
        } else if (value instanceof Boolean) {
            sb.append(((Boolean) value).booleanValue() ? "true" : "false");
        } else if (value instanceof Integer || value instanceof Long
                || value instanceof Short || value instanceof Byte) {
            sb.append(((Number) value).longValue());
        } else if (value instanceof Number) {
            double d = ((Number) value).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new IllegalArgumentException("non-finite number");
            }
            sb.append(d);
        } else if (value instanceof Map) {
            writeMap(sb, (Map<?, ?>) value);
        } else if (value instanceof Iterable) {
            writeIterable(sb, (Iterable<?>) value);
        } else if (value instanceof Object[]) {
            writeIterable(sb, java.util.Arrays.asList((Object[]) value));
        } else if (value instanceof int[]) {
            int[] a = (int[]) value;
            List<Object> boxed = new ArrayList<>(a.length);
            for (int v : a) {
                boxed.add(v);
            }
            writeIterable(sb, boxed);
        } else if (value instanceof long[]) {
            long[] a = (long[]) value;
            List<Object> boxed = new ArrayList<>(a.length);
            for (long v : a) {
                boxed.add(v);
            }
            writeIterable(sb, boxed);
        } else if (value instanceof double[]) {
            double[] a = (double[]) value;
            List<Object> boxed = new ArrayList<>(a.length);
            for (double v : a) {
                boxed.add(v);
            }
            writeIterable(sb, boxed);
        } else if (value instanceof boolean[]) {
            boolean[] a = (boolean[]) value;
            List<Object> boxed = new ArrayList<>(a.length);
            for (boolean v : a) {
                boxed.add(v);
            }
            writeIterable(sb, boxed);
        } else {
            throw new IllegalArgumentException(
                "cannot encode " + value.getClass().getName());
        }
    }

    private static void writeMap(StringBuilder sb, Map<?, ?> map) {
        sb.append('{');
        boolean first = true;
        for (Map.Entry<?, ?> e : map.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            writeString(sb, String.valueOf(e.getKey()));
            sb.append(':');
            write(sb, e.getValue());
        }
        sb.append('}');
    }

    private static void writeIterable(StringBuilder sb, Iterable<?> items) {
        sb.append('[');
        boolean first = true;
        for (Object item : items) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            write(sb, item);
        }
        sb.append(']');
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':
                    sb.append("\\\"");
                    break;
                case '\\':
                    sb.append("\\\\");
                    break;
                case '\n':
                    sb.append("\\n");
                    break;
                case '\r':
                    sb.append("\\r");
                    break;
                case '\t':
                    sb.append("\\t");
                    break;
                case '\b':
                    sb.append("\\b");
                    break;
                case '\f':
                    sb.append("\\f");
                    break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
    }

    // ── decode ────────────────────────────────────────────────────────────

    static Object decode(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object value = p.value();
        p.skipWs();
        if (p.pos != text.length()) {
            throw new IllegalArgumentException("trailing characters at " + p.pos);
        }
        return value;
    }

    /** The decoded object as a map; anything else is a protocol error. */
    @SuppressWarnings("unchecked")
    static Map<String, Object> asObject(Object value) {
        if (value instanceof Map) {
            return (Map<String, Object>) value;
        }
        throw new IllegalArgumentException("not a JSON object");
    }

    /** The decoded array as a list; anything else is a protocol error. */
    @SuppressWarnings("unchecked")
    static List<Object> asArray(Object value) {
        if (value instanceof List) {
            return (List<Object>) value;
        }
        throw new IllegalArgumentException("not a JSON array");
    }

    private static final class Parser {
        private final String s;
        private int pos;

        Parser(String s) {
            this.s = s;
        }

        void skipWs() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\n' || c == '\r' || c == '\t') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        private char peek() {
            if (pos >= s.length()) {
                throw new IllegalArgumentException("unexpected end of JSON");
            }
            return s.charAt(pos);
        }

        private void expect(char c) {
            if (peek() != c) {
                throw new IllegalArgumentException("expected '" + c + "' at " + pos);
            }
            pos++;
        }

        Object value() {
            char c = peek();
            switch (c) {
                case '{':
                    return object();
                case '[':
                    return array();
                case '"':
                    return string();
                case 't':
                    literal("true");
                    return Boolean.TRUE;
                case 'f':
                    literal("false");
                    return Boolean.FALSE;
                case 'n':
                    literal("null");
                    return null;
                default:
                    return number();
            }
        }

        private void literal(String word) {
            if (!s.startsWith(word, pos)) {
                throw new IllegalArgumentException("bad literal at " + pos);
            }
            pos += word.length();
        }

        private Map<String, Object> object() {
            Map<String, Object> out = new LinkedHashMap<>();
            expect('{');
            skipWs();
            if (peek() == '}') {
                pos++;
                return out;
            }
            while (true) {
                skipWs();
                String key = string();
                skipWs();
                expect(':');
                skipWs();
                out.put(key, value());
                skipWs();
                char c = peek();
                if (c == ',') {
                    pos++;
                } else if (c == '}') {
                    pos++;
                    return out;
                } else {
                    throw new IllegalArgumentException("expected ',' or '}' at " + pos);
                }
            }
        }

        private List<Object> array() {
            List<Object> out = new ArrayList<>();
            expect('[');
            skipWs();
            if (peek() == ']') {
                pos++;
                return out;
            }
            while (true) {
                skipWs();
                out.add(value());
                skipWs();
                char c = peek();
                if (c == ',') {
                    pos++;
                } else if (c == ']') {
                    pos++;
                    return out;
                } else {
                    throw new IllegalArgumentException("expected ',' or ']' at " + pos);
                }
            }
        }

        private String string() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                char c = peek();
                pos++;
                if (c == '"') {
                    return sb.toString();
                }
                if (c != '\\') {
                    sb.append(c);
                    continue;
                }
                char e = peek();
                pos++;
                switch (e) {
                    case '"':
                        sb.append('"');
                        break;
                    case '\\':
                        sb.append('\\');
                        break;
                    case '/':
                        sb.append('/');
                        break;
                    case 'b':
                        sb.append('\b');
                        break;
                    case 'f':
                        sb.append('\f');
                        break;
                    case 'n':
                        sb.append('\n');
                        break;
                    case 'r':
                        sb.append('\r');
                        break;
                    case 't':
                        sb.append('\t');
                        break;
                    case 'u':
                        if (pos + 4 > s.length()) {
                            throw new IllegalArgumentException("bad \\u escape at " + pos);
                        }
                        sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                        pos += 4;
                        break;
                    default:
                        throw new IllegalArgumentException("bad escape at " + pos);
                }
            }
        }

        private Object number() {
            int start = pos;
            boolean integral = true;
            if (peek() == '-') {
                pos++;
            }
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c >= '0' && c <= '9') {
                    pos++;
                } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                    integral = false;
                    pos++;
                } else {
                    break;
                }
            }
            String text = s.substring(start, pos);
            if (text.isEmpty() || "-".equals(text)) {
                throw new IllegalArgumentException("bad number at " + start);
            }
            try {
                if (integral) {
                    return Long.valueOf(text);
                }
                return Double.valueOf(text);
            } catch (NumberFormatException ex) {
                throw new IllegalArgumentException("bad number at " + start);
            }
        }
    }
}
