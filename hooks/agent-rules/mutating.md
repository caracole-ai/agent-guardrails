[Règles harnais — mutating]
Ces règles complètent ton prompt ; en cas de conflit sur le format de sortie, le prompt prime.
- Coherence first : avant d'écrire, lis comment le codebase résout déjà un problème similaire (un sibling + le point d'appel) et suis ses patterns (nommage, structure, flow).
- No hardcoded values : importe depuis la source de vérité (enum, config, constante) ; si elle n'existe pas, crée-la.
- Vérifie types et enums à la source avant usage, jamais de mémoire.
- Surgical changes : chaque ligne modifiée trace à la demande, pas d'amélioration adjacente ; dead code préexistant = mentionner, pas supprimer ; imports orphelinés par tes changements = nettoyer.
- Fallback silencieux d'env var côté serveur (`process.env.X ?? "…"`) = anti-pattern : throw au boot (`assertEnv`) ou gater `NODE_ENV !== 'production'`.
- Termine ton rapport par **Vérifié** (surfaces concrètes : chemins, commandes, tests passés) et **Non vérifié, risque** (ce qui n'a pas été testé ; si vide, justifie en une phrase). Attendu par le processus parent.
