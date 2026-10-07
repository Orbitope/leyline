package com.acme;

public class App {
    public static void main(String[] args) {
        Counter c = new Counter(5);
        c.add(2);
        System.out.println(c.get());
    }

    static void words(Counter c) {
        String label = "two";
        c.add(label);
        c.add(new Counter());
    }

    static int chained() {
        return new Counter().plus(1).get();
    }

    static void again(Counter c) {
        c.run();
    }
}
