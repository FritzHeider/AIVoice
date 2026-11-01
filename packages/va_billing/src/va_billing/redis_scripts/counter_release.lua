-- counter_release.lua (specs/24 §6 rollback): KEYS[1] counter.
-- Decrements only an existing positive counter, so a rollback never creates a key without a TTL.
-- Returns the counter after the call (0 when the key is absent).
local value = tonumber(redis.call('GET', KEYS[1]))
if value == nil then
  return 0
end
if value > 0 then
  return redis.call('DECR', KEYS[1])
end
return value
