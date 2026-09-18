module Main where

import System.Exit (ExitCode(..), exitWith)
import System.IO (hPutStrLn, stderr)

jsonEscape :: String -> String
jsonEscape = concatMap escape
  where
    escape '"' = "\\\""
    escape '\\' = "\\\\"
    escape '\n' = "\\n"
    escape '\r' = "\\r"
    escape '\t' = "\\t"
    escape c = [c]

json :: String -> String -> String
json decision reason = "{\"decision\":\"" ++ jsonEscape decision
  ++ "\",\"reason\":\"" ++ jsonEscape reason ++ "\"}"

malformed :: String -> IO ()
malformed reason = do
  putStrLn (json "reject" reason)
  hPutStrLn stderr reason
  exitWith (ExitFailure 2)

decide :: String -> String -> String -> String -> String
decide evidence observation rollback human
  | evidence == "invalid" || evidence == "missing" = "reject"
  | observation == "security" = "contain_and_escalate"
  | observation == "unknown" = "investigate"
  | observation == "regression" && rollback == "compatible" = "rollback"
  | observation == "regression" = "contain_and_escalate"
  | observation == "healthy" && human == "reject" = "reject"
  | observation == "healthy" = "promote"
  | otherwise = "reject"

reasonFor :: String -> String -> String -> String -> String -> String
reasonFor evidence observation rollback human decision
  | evidence == "invalid" || evidence == "missing" = "evidence is not valid"
  | observation == "security" = "security observation requires containment and escalation"
  | observation == "unknown" = "unknown observation cannot promote"
  | observation == "regression" && rollback == "compatible" = "regression with compatible rollback"
  | observation == "regression" = "regression without compatible rollback"
  | observation == "healthy" && human == "reject" = "human rejection overrides healthy observation"
  | observation == "healthy" = "healthy observation with valid evidence"
  | otherwise = "invalid decision state: " ++ decision ++ " for " ++ human

valid :: [String] -> Bool
valid fields = case fields of
  ["v1", evidence, observation, rollback, human] ->
    evidence `elem` ["valid", "invalid", "missing"]
      && observation `elem` ["healthy", "regression", "security", "unknown"]
      && rollback `elem` ["compatible", "incompatible"]
      && human `elem` ["none", "approve", "reject"]
  _ -> False

main :: IO ()
main = do
  input <- getContents
  case lines input of
    [line] ->
      let fields = splitTabs line
      in if not (valid fields)
           then malformed "malformed v1 policy record"
           else case fields of
             [_, evidence, observation, rollback, human] -> do
               let decision = decide evidence observation rollback human
               putStrLn (json decision (reasonFor evidence observation rollback human decision))
             _ -> malformed "malformed v1 policy record"
    _ -> malformed "expected exactly one TSV policy record"
  where
    splitTabs [] = [""]
    splitTabs value = go value ""
    go [] current = [reverse current]
    go ('\t':rest) current = reverse current : go rest ""
    go (character:rest) current = go rest (character:current)
