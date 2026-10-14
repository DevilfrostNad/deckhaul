"""Small HTML samples shaped like TruckyMods pages (structure only, invented content)."""

SEARCH = """
<div class="col-3"> <div class="card project-card card-bordered"> <div class="mx-6">
 <div class="info-overlay category-overlay"><a href="https://truckymods.io/euro-truck-simulator-2/sounds">Sounds</a></div>
 <div class="info-overlay rating-overlay"> <i class="bi bi-star-fill on text-premium"></i> <span class="text-premium">4.8</span> </div>
 <div class="info-overlay download-overlay"><div class="percent text-success"> <i class="bi bi-download"></i> 1,234</div></div>
 <a href=https://truckymods.io/euro-truck-simulator-2/sounds/test-engine-sound title="Test Engine Sound"> <img class="card-img lazy" src="/p.png" data-src="https://storage.truckymods.io/x/1.webp" alt="Test Engine Sound"></a> </div>
 <div class="card-body"> <a href="https://truckymods.io/user/1" class="text-uppercase"><i class="bi bi-person"></i> Sound Maker</a>
 <div class="card-title fs-2"> <a class="text-white" href=https://truckymods.io/euro-truck-simulator-2/sounds/test-engine-sound title="Test Engine Sound"> Test Engine Sound </a> </div> </div> </div> </div>
<div class="col-3"> <div class="card project-card card-bordered"> <div class="mx-6">
 <a href=https://truckymods.io/american-truck-simulator/maps/other-game-map title="Other Game Map">x</a> </div> </div> </div>
"""

CATEGORY = """
<div class="card mb-3"> <div class="card-body"> <div class="row no-gutters"> <div class="col-12">
 <a href="https://truckymods.io/euro-truck-simulator-2/maps/test-map-123"><img class="lazy" src="/p.png" data-src="https://storage.truckymods.io/x/2.webp" alt="Test Map"></a> </div>
 <div class="col"> <a class="fs-1" title="Test Map &amp; More" href="https://truckymods.io/euro-truck-simulator-2/maps/test-map-123">Test Map &amp; More</a>
 <span class="author-name fs-5">by</span> <a href="https://truckymods.io/user/2" class="author-name fs-4"> Map Author</a>
 <div class="mt-3"> A small test map. </div>
 <p class="quick-description"> <span class="downloads"><i class="bi bi-download text-success"></i> 99,000</span> <span title="4.5 stars"><i class="bi bi-star-fill on"></i> 4.5</span> </p>
 </div> </div> </div> </div>
"""

MOD = """
<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"WebPage","name":"Test Map for Euro Truck Simulator 2","description":"A map for tests.","url":"https://truckymods.io/euro-truck-simulator-2/maps/test-map-123","image":["https://storage.truckymods.io/x/hero.webp"]}</script>
</head><body>
<a class="text-gray-900 text-hover-primary fs-6 fw-bolder"
   href="https://truckymods.io/user/2">Map Author</a>
<div class="fw-bolder mt-5">Current Version</div> <div class="text-gray-600"><span class="badge badge-primary">2.1</span></div>
<div class="fw-bolder mt-5">Compatibility</div> <div class="text-gray-60"><span class="badge badge-primary me-1">1.53</span> <span class="badge badge-primary me-1">1.54</span> </div>
<div class="fw-bolder mt-5">Last Updated</div> <div class="text-gray-600">2026-05-01 </div>
<div class="fw-bolder mt-5">Downloads</div> <div class="text-success fs-2 fw-bolder">12,345</div>
<downloadbutton :beta="false" :project_id="77" :build_id="902" :size="5000" download_permitted_to="all" :auth="false" :download_url="'https://truckymods.io/euro-truck-simulator-2/maps/test-map-123/download/latest'"></downloadbutton>
<div class="tab-pane fade " id="nav-builds" role="tabpanel"> <div class="timeline-label builds">
 <div class="timeline-item"> <div class="timeline-label timeline-date"> <span class="badge badge-light-primary mb-1 fs-4">2.1</span> <br /> 2026-05-01
  <downloadbutton :build_id="902" :download_url="'https://truckymods.io/euro-truck-simulator-2/maps/test-map-123/download/902'"></downloadbutton> </div>
  <div class="timeline-content"> <div> Supported Game Versions: <span class="badge badge-primary me-1">1.54</span> <span class="text-success"><i class="bi bi-download"></i> 50</span> </div>
  <div class="mt-3"> <p>Changes:</p><p>New roads.</p> </div> </div> </div>
 <div class="timeline-item"> <div class="timeline-label timeline-date"> <span class="badge badge-light-primary mb-1 fs-4">2.0</span> <br /> 2026-01-01
  <downloadbutton :build_id="801" :download_url="'https://truckymods.io/euro-truck-simulator-2/maps/test-map-123/download/801'"></downloadbutton> </div>
  <div class="timeline-content"> <div> Supported Game Versions: <span class="badge badge-primary me-1">1.53</span> </div> </div> </div>
</div> </div>
<div class="tab-pane fade" id="nav-deps" role="tabpanel">
 <a title="Going East!"
    href="https://store.steampowered.com/app/227310/?utm_source=truckymods" target="_blank">x</a>
 <a href="https://truckymods.io/euro-truck-simulator-2/maps/base-pack-1">Base Pack</a>
</div>
<div class="tab-pane fade" id="nav-comments"></div>
</body></html>
"""
