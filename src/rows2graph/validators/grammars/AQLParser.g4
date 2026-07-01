/*
 * AQL (ArangoDB Query Language) parser.
 *
 * Hand-port of ArangoDB's own Bison grammar (arangod/Aql/grammar.y, Apache-2.0),
 * pinned to the 3.11 branch that the managed validator provisions
 * (arangodb:3.11). ArangoDB publishes no reusable offline grammar, so this
 * reproduces the rule structure for deployment-free *syntax* validation only.
 *
 * Bison is LALR(1) with an explicit precedence table; the scattered
 * operator_unary / operator_binary / operator_ternary / reference rules are
 * folded here into one directly left-recursive `expression` rule whose
 * alternatives are ordered by that precedence ladder (highest first). All
 * semantic actions are dropped. Minor, deliberate over-acceptance is possible
 * (ANTLR has no %nonassoc); the server validator remains authoritative.
 *
 * Provenance: https://github.com/arangodb/arangodb/blob/3.11/arangod/Aql/grammar.y
 */
parser grammar AQLParser;

options { tokenVocab = AQLLexer; }

// Entry rule. Anchors EOF so trailing garbage after a valid prefix is reported.
queryStart
  : optionalWith query EOF
  ;

optionalWith
  : /* empty */
  | T_WITH withCollectionList
  ;

withCollectionList
  : withCollection (T_COMMA? withCollection)*
  ;

withCollection
  : T_STRING
  | bindParameterDatasourceExpected
  ;

query
  : statementBlockStatement* finalStatement
  ;

finalStatement
  : returnStatement
  | removeStatement
  | insertStatement
  | updateStatement
  | replaceStatement
  | upsertStatement
  ;

statementBlockStatement
  : forStatement
  | letStatement
  | filterStatement
  | collectStatement
  | sortStatement
  | limitStatement
  | windowStatement
  | removeStatement
  | insertStatement
  | updateStatement
  | replaceStatement
  | upsertStatement
  ;

// ---------------------------------------------------------------------------
// FOR (collection/view loops and graph traversals)
// ---------------------------------------------------------------------------
forStatement
  : T_FOR forOutputVariables T_IN forSource
  ;

forOutputVariables
  : variableName (T_COMMA variableName)*
  ;

forSource
  : traversalGraphInfo pruneAndOptions
  | shortestPathGraphInfo
  | kShortestPathsGraphInfo
  | kPathsGraphInfo
  | allShortestPathsGraphInfo
  | expression forOptions
  ;

forOptions
  : ( T_STRING expression ( T_STRING expression )? )?
  ;

traversalGraphInfo
  : graphDirectionSteps expression graphSubject
  ;

shortestPathGraphInfo
  : graphDirection T_SHORTEST_PATH expression T_STRING expression graphSubject optionsClause
  ;

kShortestPathsGraphInfo
  : graphDirection T_K_SHORTEST_PATHS expression T_STRING expression graphSubject optionsClause
  ;

kPathsGraphInfo
  : graphDirectionSteps T_K_PATHS expression T_STRING expression graphSubject optionsClause
  ;

allShortestPathsGraphInfo
  : graphDirection T_ALL_SHORTEST_PATHS expression T_STRING expression graphSubject optionsClause
  ;

pruneAndOptions
  : ( T_STRING optionalPruneVariable ( T_STRING object )? )?
  ;

optionalPruneVariable
  : variableName T_ASSIGN expression
  | expression
  ;

graphSubject
  : graphCollection ( T_COMMA graphCollectionList )?
  | T_GRAPH ( bindParameter | T_QUOTED_STRING | T_STRING )
  ;

graphCollectionList
  : graphCollection ( T_COMMA graphCollection )*
  ;

graphCollection
  : graphDirection ( T_STRING | bindParameter )
  | T_STRING
  | bindParameterDatasourceExpected
  ;

graphDirection
  : T_OUTBOUND
  | T_INBOUND
  | T_ANY
  ;

graphDirectionSteps
  : graphDirection
  | expression graphDirection
  ;

// ---------------------------------------------------------------------------
// LET / FILTER
// ---------------------------------------------------------------------------
letStatement
  : T_LET letList
  ;

letList
  : letElement ( T_COMMA letElement )*
  ;

letElement
  : variableName T_ASSIGN expression
  ;

filterStatement
  : T_FILTER expression
  ;

// ---------------------------------------------------------------------------
// COLLECT
// ---------------------------------------------------------------------------
collectStatement
  : T_COLLECT countInto optionsClause
  | T_COLLECT collectList countInto optionsClause
  | T_COLLECT aggregate collectOptionalInto optionsClause
  | T_COLLECT collectList aggregate collectOptionalInto optionsClause
  | T_COLLECT collectList collectOptionalInto keep? optionsClause
  ;

countInto
  : T_WITH T_STRING T_INTO variableName
  ;

collectList
  : collectElement ( T_COMMA collectElement )*
  ;

collectElement
  : variableName T_ASSIGN expression
  ;

collectOptionalInto
  : /* empty */
  | T_INTO variableName ( T_ASSIGN expression )?
  ;

keep
  : T_STRING variableList
  ;

variableList
  : variableName ( T_COMMA variableName )*
  ;

aggregate
  : T_AGGREGATE aggregateList
  ;

aggregateList
  : aggregateElement ( T_COMMA aggregateElement )*
  ;

aggregateElement
  : variableName T_ASSIGN aggregateFunctionCall
  ;

aggregateFunctionCall
  : functionName T_OPEN optionalFunctionCallArguments T_CLOSE
  ;

// ---------------------------------------------------------------------------
// SORT / LIMIT / WINDOW / RETURN
// ---------------------------------------------------------------------------
sortStatement
  : T_SORT sortList
  ;

sortList
  : sortElement ( T_COMMA sortElement )*
  ;

sortElement
  : expression sortDirection
  ;

sortDirection
  : /* empty */
  | T_ASC
  | T_DESC
  | simpleValue
  ;

limitStatement
  : T_LIMIT expression ( T_COMMA expression )?
  ;

windowStatement
  : T_WINDOW object aggregate
  | T_WINDOW expression T_WITH object aggregate
  ;

returnStatement
  : T_RETURN T_DISTINCT? expression
  ;

// ---------------------------------------------------------------------------
// data-modification statements
// ---------------------------------------------------------------------------
removeStatement
  : T_REMOVE expression inOrIntoCollection optionsClause
  ;

insertStatement
  : T_INSERT expression inOrIntoCollection optionsClause
  ;

updateStatement
  : T_UPDATE updateParameters
  ;

updateParameters
  : expression ( T_WITH expression )? inOrIntoCollection optionsClause
  ;

replaceStatement
  : T_REPLACE replaceParameters
  ;

replaceParameters
  : expression ( T_WITH expression )? inOrIntoCollection optionsClause
  ;

upsertStatement
  : T_UPSERT expression T_INSERT expression updateOrReplace expression inOrIntoCollection optionsClause
  ;

updateOrReplace
  : T_UPDATE
  | T_REPLACE
  ;

inOrIntoCollection
  : ( T_IN | T_INTO ) inOrIntoCollectionName
  ;

inOrIntoCollectionName
  : T_STRING
  | T_QUOTED_STRING
  | T_DATA_SOURCE_PARAMETER
  ;

// OPTIONS { ... } trailer (the leading T_STRING is the "OPTIONS" qualifier).
optionsClause
  : /* empty */
  | T_STRING object
  ;

// ---------------------------------------------------------------------------
// expressions. One left-recursive rule; alternatives ordered by the Bison
// precedence ladder, highest precedence first.
// ---------------------------------------------------------------------------
expression
  : ( T_PLUS | T_MINUS | T_NOT ) expression                                       # unaryExpression
  | expression ( T_TIMES | T_DIV | T_MOD ) expression                             # binaryExpression
  | expression ( T_PLUS | T_MINUS ) expression                                    # binaryExpression
  | expression T_RANGE expression                                                 # rangeExpression
  | expression ( T_LT | T_GT | T_LE | T_GE ) expression                           # binaryExpression
  | expression ( T_IN | T_NOT T_IN ) expression                                   # binaryExpression
  | expression
      ( T_EQ | T_NE | T_LIKE | T_REGEX_MATCH | T_REGEX_NON_MATCH
      | T_NOT T_LIKE | T_NOT T_REGEX_MATCH | T_NOT T_REGEX_NON_MATCH ) expression  # binaryExpression
  | expression quantifier
      ( T_EQ | T_NE | T_LT | T_GT | T_LE | T_GE | T_IN | T_NOT T_IN ) expression   # arrayComparisonExpression
  | expression T_AT_LEAST T_OPEN expression T_CLOSE
      ( T_EQ | T_NE | T_LT | T_GT | T_LE | T_GE | T_IN | T_NOT T_IN ) expression   # atLeastComparisonExpression
  | expression T_AND expression                                                   # binaryExpression
  | expression T_OR expression                                                    # binaryExpression
  | <assoc=right> expression T_QUESTION expression? T_COLON expression            # ternaryExpression
  | valueLiteral                                                                  # literalExpression
  | reference                                                                     # referenceExpression
  ;

quantifier
  : T_ALL
  | T_ANY
  | T_NONE
  ;

// reference: primaries plus left-recursive postfix access / expansion.
reference
  : functionCall                                                                  # functionCallReference
  | T_STRING                                                                       # variableReference
  | compoundValue                                                                  # compoundReference
  | bindParameter                                                                  # bindParameterReference
  | T_OPEN expression T_CLOSE                                                       # parenthesizedReference
  | T_OPEN query T_CLOSE                                                            # subqueryReference
  | reference DOT T_STRING                                                          # attributeAccess
  | reference DOT bindParameter                                                     # boundAttributeAccess
  | reference T_ARRAY_OPEN expression T_ARRAY_CLOSE                                 # indexedAccess
  | reference T_ARRAY_OPEN arrayFilterOperator optionalArrayFilter T_ARRAY_CLOSE   # booleanExpansion
  | reference T_ARRAY_OPEN arrayMapOperator optionalArrayFilter optionalArrayLimit optionalArrayReturn T_ARRAY_CLOSE # arrayExpansion
  ;

functionCall
  : functionName T_OPEN optionalFunctionCallArguments T_CLOSE
  | T_LIKE T_OPEN optionalFunctionCallArguments T_CLOSE
  ;

functionName
  : T_STRING ( T_SCOPE T_STRING )*
  ;

optionalFunctionCallArguments
  : /* empty */
  | functionArgumentsList
  ;

functionArgumentsList
  : expressionOrQuery ( T_COMMA expressionOrQuery )*
  ;

expressionOrQuery
  : expression
  | query
  ;

arrayFilterOperator
  : T_QUESTION+
  ;

arrayMapOperator
  : T_TIMES+
  ;

optionalArrayFilter
  : /* empty */
  | T_FILTER expression
  | quantifier T_FILTER expression
  | T_AT_LEAST T_OPEN expression T_CLOSE T_FILTER expression
  | expression T_FILTER expression
  ;

optionalArrayLimit
  : /* empty */
  | T_LIMIT expression ( T_COMMA expression )?
  ;

optionalArrayReturn
  : /* empty */
  | T_RETURN expression
  ;

// ---------------------------------------------------------------------------
// compound values, literals, bind parameters
// ---------------------------------------------------------------------------
compoundValue
  : array
  | object
  ;

array
  : T_ARRAY_OPEN optionalArrayElements T_ARRAY_CLOSE
  ;

optionalArrayElements
  : /* empty */
  | arrayElementsList T_COMMA?
  ;

arrayElementsList
  : arrayElement ( T_COMMA arrayElement )*
  ;

arrayElement
  : expression
  ;

object
  : T_OBJECT_OPEN optionalObjectElements T_OBJECT_CLOSE
  ;

optionalObjectElements
  : /* empty */
  | objectElementsList T_COMMA?
  ;

objectElementsList
  : objectElement ( T_COMMA objectElement )*
  ;

objectElement
  : objectElementName T_COLON expression
  | T_PARAMETER T_COLON expression
  | T_ARRAY_OPEN expression T_ARRAY_CLOSE T_COLON expression
  | T_STRING
  ;

objectElementName
  : T_STRING
  | T_QUOTED_STRING
  ;

simpleValue
  : valueLiteral
  | bindParameter
  ;

valueLiteral
  : T_QUOTED_STRING
  | numericValue
  | T_NULL
  | T_TRUE
  | T_FALSE
  ;

numericValue
  : T_INTEGER
  | T_DOUBLE
  ;

bindParameter
  : T_DATA_SOURCE_PARAMETER
  | T_PARAMETER
  ;

bindParameterDatasourceExpected
  : T_DATA_SOURCE_PARAMETER
  | T_PARAMETER
  ;

variableName
  : T_STRING
  ;
