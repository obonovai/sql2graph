/*
 * AQL (ArangoDB Query Language) lexer.
 *
 * This is a hand-port of ArangoDB's own Flex lexer (arangod/Aql/tokens.ll,
 * Apache-2.0), pinned to the 3.11 branch that the managed validator provisions
 * (arangodb:3.11). It is NOT ArangoDB's own grammar file: ArangoDB's parser is
 * hand-written C++/Flex+Bison with no reusable offline grammar, so this port
 * reproduces the token structure for deployment-free *syntax* validation only.
 * Semantic actions (AST building, number-range/escape validation) are dropped;
 * the server validator remains authoritative. See grammars/README.md.
 *
 * Provenance: https://github.com/arangodb/arangodb/blob/3.11/arangod/Aql/tokens.ll
 */
lexer grammar AQLLexer;

// AQL keywords and the boolean/null literals are case-insensitive, matching the
// (?i:...) rules in tokens.ll. String/identifier *content* is unaffected.
options { caseInsensitive = true; }

// ---------------------------------------------------------------------------
// language keywords
// ---------------------------------------------------------------------------
T_FOR:                'FOR';
T_LET:                'LET';
T_FILTER:             'FILTER';
T_RETURN:             'RETURN';
T_COLLECT:            'COLLECT';
T_SORT:               'SORT';
T_LIMIT:              'LIMIT';
T_WINDOW:             'WINDOW';
T_DISTINCT:           'DISTINCT';
T_AGGREGATE:          'AGGREGATE';
T_ASC:                'ASC';
T_DESC:               'DESC';
// "AT LEAST" is a single token in tokens.ll (AT[ \t\r\n]+LEAST); reproduced here
// with the same lack of a trailing-word guard.
T_AT_LEAST:           'AT' [ \t\r\n]+ 'LEAST';
T_IN:                 'IN';
T_INTO:               'INTO';
T_WITH:               'WITH';
T_REMOVE:             'REMOVE';
T_INSERT:             'INSERT';
T_UPDATE:             'UPDATE';
T_REPLACE:            'REPLACE';
T_UPSERT:             'UPSERT';
T_GRAPH:              'GRAPH';
T_SHORTEST_PATH:      'SHORTEST_PATH';
T_K_SHORTEST_PATHS:   'K_SHORTEST_PATHS';
T_ALL_SHORTEST_PATHS: 'ALL_SHORTEST_PATHS';
T_K_PATHS:            'K_PATHS';
T_OUTBOUND:           'OUTBOUND';
T_INBOUND:            'INBOUND';
T_ANY:                'ANY';
T_ALL:                'ALL';
T_NONE:               'NONE';
T_LIKE:               'LIKE';

// keyword/word operators (symbol forms handled below). "NOT IN" is reassembled
// in the parser as T_NOT T_IN; standalone IN only matches as a full token
// (identifiers are maximal-munch), reproducing tokens.ll's trailing-word guard.
T_NOT:                'NOT' | '!';
T_AND:                'AND' | '&&';
T_OR:                 'OR'  | '||';

// predefined type literals
T_NULL:               'NULL';
T_TRUE:               'TRUE';
T_FALSE:              'FALSE';

// ---------------------------------------------------------------------------
// operators (multi-char forms win by maximal munch)
// ---------------------------------------------------------------------------
T_REGEX_MATCH:        '=~';
T_REGEX_NON_MATCH:    '!~';
T_EQ:                 '==';
T_NE:                 '!=';
T_GE:                 '>=';
T_GT:                 '>';
T_LE:                 '<=';
T_LT:                 '<';
T_ASSIGN:             '=';
T_PLUS:               '+';
T_MINUS:              '-';
T_TIMES:              '*';
T_DIV:                '/';
T_MOD:                '%';
T_QUESTION:           '?';
T_SCOPE:              '::';
T_COLON:              ':';
T_RANGE:              '..';

// ---------------------------------------------------------------------------
// punctuation
// ---------------------------------------------------------------------------
T_COMMA:              ',';
T_OPEN:               '(';
T_CLOSE:              ')';
T_OBJECT_OPEN:        '{';
T_OBJECT_CLOSE:       '}';
T_ARRAY_OPEN:         '[';
T_ARRAY_CLOSE:        ']';
// bare '.' is returned as a character token by tokens.ll's catch-all and used by
// the grammar for attribute access (reference '.' T_STRING).
DOT:                  '.';

// ---------------------------------------------------------------------------
// number literals (integer rules precede the double rule so a plain integer
// wins the equal-length tie; longer decimals/hex/binary win by maximal munch)
// ---------------------------------------------------------------------------
// (`caseInsensitive` makes lower-case sets match upper case too, so [a-f]/[b]/[x]
// below also accept A-F/B/X.)
T_INTEGER
  : '0'
  | [1-9] [0-9]*
  | '0' 'b' [01]+
  | '0' 'x' [0-9a-f]+
  ;

T_DOUBLE
  : ('0' | [1-9] [0-9]*) ('.' [0-9]+)? EXPONENT?
  | '.' [0-9]+ EXPONENT?
  ;

fragment EXPONENT: 'e' [+\-]? [0-9]+;

// ---------------------------------------------------------------------------
// string literals (single/double quoted) and quoted identifiers (backtick /
// forwardtick). Both quote styles may span newlines and honour backslash
// escapes, matching the lexer states in tokens.ll.
// ---------------------------------------------------------------------------
T_QUOTED_STRING
  : '"'  ( '\\' . | ~["\\]  )* '"'
  | '\'' ( '\\' . | ~['\\]  )* '\''
  ;

// unquoted identifier plus backtick/forwardtick-quoted identifiers, all T_STRING
T_STRING
  : ('$' | '_'+)? [a-z]+ [_a-z0-9]*
  | '`'      ( '\\' . | ~[`\\] )*      '`'
  | '´' ( '\\' . | ~[´\\] )* '´'
  ;

// ---------------------------------------------------------------------------
// bind parameters (@name) and data-source bind parameters (@@name).
// @@ is listed first so it is preferred; @ cannot match "@@..." anyway.
// ---------------------------------------------------------------------------
T_DATA_SOURCE_PARAMETER: '@@' ( '_'+ [a-z0-9]+ [a-z0-9_]* | [a-z0-9] [a-z0-9_]* );
T_PARAMETER:             '@'  ( '_'+ [a-z0-9]+ [a-z0-9_]* | [a-z0-9] [a-z0-9_]* );

// ---------------------------------------------------------------------------
// comments and whitespace (skipped). Block comments are non-nesting, matching
// tokens.ll; an unterminated block comment fails to match and is reported.
// ---------------------------------------------------------------------------
LINE_COMMENT:  '//' ~[\n]*      -> skip;
BLOCK_COMMENT: '/*' .*? '*/'    -> skip;
WS:            [ \t\r\n]+       -> skip;
