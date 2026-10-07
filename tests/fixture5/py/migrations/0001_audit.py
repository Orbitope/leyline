def upgrade(op):
    op.execute("INSERT INTO audit (item) VALUES ('seed')")
