CREATE TABLE region (
    regionkey   INTEGER PRIMARY KEY,
    name        VARCHAR(25),
    comment     VARCHAR(152)
);

CREATE TABLE nation (
    nationkey   INTEGER PRIMARY KEY,
    name        VARCHAR(25),
    comment     VARCHAR(152),
    regionkey   INTEGER REFERENCES region(regionkey)
);

CREATE TABLE supplier (
    suppkey     INTEGER PRIMARY KEY,
    name        VARCHAR(25),
    address     VARCHAR(40),
    phone       VARCHAR(15),
    acctbal     DECIMAL(15, 2),
    comment     VARCHAR(101),
    nationkey   INTEGER REFERENCES nation(nationkey)
);

CREATE TABLE customer (
    custkey     INTEGER PRIMARY KEY,
    name        VARCHAR(25),
    address     VARCHAR(40),
    phone       VARCHAR(15),
    acctbal     DECIMAL(15, 2),
    mktsegment  VARCHAR(10),
    comment     VARCHAR(117),
    nationkey   INTEGER REFERENCES nation(nationkey)
);

CREATE TABLE part (
    partkey     INTEGER PRIMARY KEY,
    name        VARCHAR(55),
    mfgr        VARCHAR(25),
    brand       VARCHAR(10),
    type        VARCHAR(25),
    size        INTEGER,
    container   VARCHAR(10),
    retailprice DECIMAL(15, 2),
    comment     VARCHAR(23)
);

CREATE TABLE partsupp (
    partkey     INTEGER REFERENCES part(partkey),
    suppkey     INTEGER REFERENCES supplier(suppkey),
    availqty    INTEGER,
    supplycost  DECIMAL(15, 2),
    comment     VARCHAR(199),
    PRIMARY KEY (partkey, suppkey)
);

CREATE TABLE orders (
    orderkey      INTEGER PRIMARY KEY,
    orderstatus   CHAR(1),
    totalprice    DECIMAL(15, 2),
    orderdate     DATE,
    orderpriority VARCHAR(15),
    clerk         VARCHAR(15),
    shippriority  INTEGER,
    comment       VARCHAR(79),
    custkey       INTEGER REFERENCES customer(custkey)
);

CREATE TABLE lineitem (
    orderkey      INTEGER REFERENCES orders(orderkey),
    partkey       INTEGER REFERENCES part(partkey),
    suppkey       INTEGER REFERENCES supplier(suppkey),
    linenumber    INTEGER,
    quantity      DECIMAL(15, 2),
    extendedprice DECIMAL(15, 2),
    discount      DECIMAL(15, 2),
    tax           DECIMAL(15, 2),
    returnflag    CHAR(1),
    linestatus    CHAR(1),
    shipdate      DATE,
    commitdate    DATE,
    receiptdate   DATE,
    shipinstruct  VARCHAR(25),
    shipmode      VARCHAR(10),
    comment       VARCHAR(44),
    PRIMARY KEY (orderkey, linenumber)
);
