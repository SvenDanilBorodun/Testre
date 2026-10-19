package edubotics;

import java.io.InputStream;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Startet das Java-Programm des Schülers und meldet einen Fehler mit Datei und Zeile.
 *
 * <p>The Java twin of {@code student_main.py} (area C, F2; hand-written like
 * SmokeMain and EduJson). The supervisor runs
 * {@code java … edubotics.Launcher Main <sources…>}: this class replaces
 * {@code System.in} with a stream that refuses in German (there is no keyboard
 * on the robot), runs {@code Main.main}, and on ANY fault — a robot refusal or
 * the student's own exception, also one thrown while a class initialises —
 * reports the exit dict {@code {kind, file, line, exc_type, name, detail_line}}
 * ({@code code_rpc._EXIT_INFO_KEYS}) as the internal {@code __exit} call, then
 * ends the JVM with code 1. {@code kind} is one of the EXISTING wire kinds
 * ({@code code_errors_de.ERROR_KINDS}), so an older server reads it too. The
 * German sentence is the server's; this class says nothing to the student
 * itself except the keyboard refusal, byte-equal with student_main's
 * (tests/test_code_runner_launcher_lockstep.py). It never prints a stack
 * trace: stderr is never shown to the student.
 *
 * <p>A clean return of {@code main} returns from here too (the JVM then ends
 * when the program's own threads do, as it did before this class); a
 * non-public {@code static void main} runs, which {@code java} itself would
 * refuse (accepted, KNOWN-ISSUES).
 */
public final class Launcher {

    static final String INPUT_DE =
        "Eingaben über die Tastatur gibt es hier nicht — das Programm läuft auf dem Roboter.";

    // code_rpc._EXIT_INFO_KEYS: exc_type and name are cut at 80, the detail at 2000.
    private static final int EXC_TYPE_MAX_CHARS = 80;
    private static final int DETAIL_MAX_CHARS = 2000;
    // Frames whose class starts with one of these are not the student's.
    private static final String[] FOREIGN_PREFIXES = {"edubotics.", "java.", "jdk.", "sun."};

    private Launcher() {
    }

    public static void main(String[] args) {
        if (args.length < 1) {
            System.exit(2);
        }
        String[] sources = java.util.Arrays.copyOfRange(args, 1, args.length);
        System.setIn(new InputStream() {
            @Override
            public int read() {
                throw new RpcClient.RobotError(INPUT_DE);
            }
        });
        Throwable fault = null;
        try {
            Class<?> cls = Class.forName(args[0]);
            Method main = cls.getDeclaredMethod("main", String[].class);
            if (!Modifier.isStatic(main.getModifiers())) {
                throw new NoSuchMethodException(args[0] + ".main(String[])");
            }
            main.setAccessible(true);
            main.invoke(null, (Object) new String[0]);
        } catch (Throwable t) {
            fault = t;
        }
        if (fault == null) {
            return;
        }
        report(classify(unwrap(fault), sources));
        System.exit(1);
    }

    /**
     * The student's own fault inside the reflection's wrappers: an
     * InvocationTargetException, an ExceptionInInitializerError (a static
     * initialiser's fault), or one inside the other.
     */
    static Throwable unwrap(Throwable t) {
        Throwable fault = t;
        for (int depth = 0; depth < 8; depth++) {
            if (!(fault instanceof InvocationTargetException
                    || fault instanceof ExceptionInInitializerError)) {
                break;
            }
            Throwable cause = fault.getCause();
            if (cause == null) {
                break;
            }
            fault = cause;
        }
        return fault;
    }

    /** The exit dict of one fault (pure; SmokeMain asserts it). */
    static Map<String, Object> classify(Throwable t, String[] sources) {
        String kind = "other";
        if (t instanceof RpcClient.RobotError) {
            kind = "robot";
        } else if (t instanceof ArithmeticException) {
            kind = "zero_division";
        } else if (t instanceof IndexOutOfBoundsException
                || t instanceof java.util.NoSuchElementException) {
            kind = "index";
        } else if (t instanceof StackOverflowError) {
            kind = "recursion";
        } else if (t instanceof OutOfMemoryError) {
            kind = "memory";
        } else if (t instanceof ClassCastException || t instanceof IllegalArgumentException) {
            kind = "type";
        }
        String file = "";
        int line = 0;
        StackTraceElement[] frames;
        try {
            frames = t.getStackTrace();
        } catch (RuntimeException e) {
            frames = new StackTraceElement[0];
        }
        for (StackTraceElement frame : frames) {
            String path = projectFile(frame, sources);
            if (path != null && frame.getLineNumber() > 0) {
                file = path;
                line = frame.getLineNumber();
                break;
            }
        }
        Map<String, Object> info = new LinkedHashMap<>();
        info.put("kind", kind);
        info.put("file", file);
        info.put("line", line);
        info.put("exc_type", cut(excType(t), EXC_TYPE_MAX_CHARS));
        info.put("name", "");
        info.put("detail_line", cut(detail(t, "robot".equals(kind)), DETAIL_MAX_CHARS));
        return info;
    }

    /**
     * The project file a frame belongs to, as the run names it ({@code
     * tiere/Hund.java}): the class's package path plus the frame's file name
     * when that is a project file, else the one project file of that base
     * name (a package declaration that does not match its directory). A frame
     * of the library, the JDK, or of no unique project file is nobody's.
     */
    static String projectFile(StackTraceElement frame, String[] sources) {
        String fileName = frame.getFileName();
        String cls = frame.getClassName();
        if (fileName == null || cls == null) {
            return null;
        }
        for (String prefix : FOREIGN_PREFIXES) {
            if (cls.startsWith(prefix)) {
                return null;
            }
        }
        int dot = cls.lastIndexOf('.');
        String path = (dot < 0 ? "" : cls.substring(0, dot).replace('.', '/') + "/") + fileName;
        String byName = null;
        int hits = 0;
        for (String source : sources) {
            if (source.equals(path)) {
                return source;
            }
            if (source.equals(fileName) || source.endsWith("/" + fileName)) {
                byName = source;
                hits++;
            }
        }
        return hits == 1 ? byName : null;
    }

    private static String excType(Throwable t) {
        String simple = t.getClass().getSimpleName();
        return simple.isEmpty() ? t.getClass().getName() : simple;
    }

    // The raw line for the [TECHNIK] entry; for a robot refusal its German
    // sentence. A student exception whose own toString/getMessage fails is
    // named by its class.
    private static String detail(Throwable t, boolean robot) {
        try {
            String text = robot ? t.getMessage() : t.toString();
            return text == null ? t.getClass().getName() : text;
        } catch (RuntimeException | StackOverflowError e) {
            return t.getClass().getName();
        }
    }

    private static String cut(String text, int max) {
        return text.length() > max ? text.substring(0, max) : text;
    }

    static void report(Map<String, Object> info) {
        try {
            RpcClient.call("__exit", new Object[] {info}, "call");
        } catch (RuntimeException e) {
            // A program that broke the library itself, or no robot: the exit
            // code 1 still says „Fehler".
        }
    }
}
