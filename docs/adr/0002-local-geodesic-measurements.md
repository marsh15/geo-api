# ADR 0002: Local measurements from WGS84 geodesic edges

**Status:** Accepted

geo-api transforms source coordinates to WGS84, treats each supplied edge as the shortest WGS84 ellipsoid geodesic, densifies, and measures in a component-centered LAEA or AEQD projection. A 100 km radius and finite coordinate/work budgets keep the v1 domain local while allowing global locations, including dateline and polar cases.

The edge model is explicit and may differ from the rendering convention of a source application. Independent GeographicLib fixture comparisons are separate from convergence checks. Expanding the radius or asserting survey accuracy requires new evidence and a revised contract.
