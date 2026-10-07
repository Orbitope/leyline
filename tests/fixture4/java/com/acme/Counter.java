package com.acme;

public class Counter {
    private int count;

    public Counter() { this(0); }
    public Counter(int start) { count = start; }

    public void add(int n) { count += n; }
    public void add(String label) { count += label.length(); }
    public void add(Counter other) { count += other.get(); }
    public Counter plus(int n) { add(n); return this; }
    public int get() { return count; }

    // Methods of an anonymous class are not the Counter's own.
    private final Runnable reset = new Runnable() {
        public void run() { count = 0; }
    };

    public void run() { reset.run(); }
}
