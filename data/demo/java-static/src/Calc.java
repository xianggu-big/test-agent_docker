public class Calc {
    // TODO 校验溢出
    public int add(int a, int b) { return a + b; }
    public int div(int a, int b) {
        try { return a / b; } catch (ArithmeticException e) { }
        return 0;
    }
    public boolean inRange(int x, int lo, int hi) { return x >= lo && x <= hi; }
}
