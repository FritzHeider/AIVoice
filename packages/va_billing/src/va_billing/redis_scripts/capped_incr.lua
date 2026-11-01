-- capped_incr.lua (specs/24 §6 items 4-5): KEYS[1] counter; ARGV cap (-1 = no cap), ttl_s.
-- Returns {ok (0|1), value}: the counter after the call; a refused increment is undone.
local cap = tonumber(ARGV[1])
local value = redis.call('INCR', KEYS[1])
if value == 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
if cap >= 0 and value > cap then
  redis.call('DECR', KEYS[1])
  return {0, value - 1}
end
return {1, value}
