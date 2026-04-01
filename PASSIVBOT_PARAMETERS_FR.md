# Passivbot v7 — Référence des Paramètres de Stratégie de Trading

Tous les paramètres se trouvent sous `config.bot.long` / `config.bot.short` (chaque côté est configuré indépendamment). Des surcharges par coin sont possibles via `config.coin_overrides`.

| # | Catégorie | Paramètre | Type | Plage | Description |
|:-:|:----------|:----------|:----:|:-----:|:------------|
| 1 | EMA | `ema_span_0` | float | 200 – 1440 | Première période EMA en minutes. Forme l'une des trois EMA servant à calculer les bandes haute/basse qui ancrent les prix d'entrée et de désengagement (unstuck). |
| 2 | EMA | `ema_span_1` | float | 200 – 1440 | Deuxième période EMA en minutes. Une troisième est dérivée automatiquement : `sqrt(ema_span_0 × ema_span_1)`. Ensemble elles définissent `ema_band_upper = max(emas)` et `ema_band_lower = min(emas)`. |
| 3 | Position | `n_positions` | int | 1 – 20+ | Nombre maximum de positions simultanées par côté. `0` désactive le côté. Chaque position reçoit `total_wallet_exposure_limit / n_positions` comme plafond individuel. |
| 4 | Position | `total_wallet_exposure_limit` | float | 0 – 10+ | Exposition totale maximale en ratio du solde non-leviérisé (`2.0` = 200%). Distance de faillite ≈ `1 / TWEL`. |
| 5 | Position | `enforce_exposure_limit` | bool | — | Si `true`, place automatiquement un ordre de clôture au marché lorsque l'exposition dépasse la limite de plus de 1%. Protège contre les retraits de solde ou changements de config. |
| 6 | Entrée Grille | `entry_initial_ema_dist` | float | −0.1 – 0.003 | Décalage par rapport à la bande EMA pour le prix d'entrée initial. Généralement négatif : place l'entrée sous la bande basse (long) ou au-dessus de la haute (short). Plus négatif = plus conservateur. |
| 7 | Entrée Grille | `entry_initial_qty_pct` | float | 0.004 – 0.1 | Taille de l'entrée initiale en fraction de `solde × wallet_exposure_limit` (ex. `0.15` = 15% de la valeur max de position). |
| 8 | Entrée Grille | `entry_grid_spacing_pct` | float | 0.001 – 0.06 | Espacement de base en % entre les niveaux de re-entry successifs, mesuré depuis le prix moyen de position. Élargi dynamiquement par les pondérations d'exposition et de volatilité. |
| 9 | Entrée Grille | `entry_grid_spacing_we_weight` | float | 0 – 10 | Influence de l'exposition courante sur l'élargissement de la grille. Plus élevé = espacement plus large à mesure que la position grandit, évitant un DCA agressif quand on est déjà chargé. |
| 10 | Entrée Grille | `entry_grid_spacing_log_weight` | float | 0 – 400 | Influence de la volatilité du marché (log-range horaire) sur l'espacement. `0` = désactivé. Des valeurs élevées adaptent la grille aux conditions volatiles. |
| 11 | Entrée Grille | `entry_grid_spacing_log_span_hours` | float | 672 – 2688 | Période EMA en heures (≈ 28–112 jours) pour lisser le signal de volatilité log-range. Plus long = plus lisse, moins réactif aux pics de court terme. |
| 12 | Entrée Grille | `entry_grid_double_down_factor` | float | 0.01 – 4 | Chaque re-entry : qté = `taille_position × ddf`. `0.5` = ajouts de moitié, `1.0` = même taille, `2.0` = doublement (martingale agressive). |
| 13 | Entrée Trailing | `entry_trailing_grid_ratio` | float | −1 – 1 | Mélange trailing/grille. `0` = grille seule, `±1` = trailing seul, `0.3` = trailing d'abord jusqu'à 30% rempli puis grille, `−0.9` = grille d'abord jusqu'à 10% rempli puis trailing. |
| 14 | Entrée Trailing | `entry_trailing_threshold_pct` | float | −0.01 – 0.1 | Le prix doit bouger de ce % depuis le prix de position pour activer le suivi trailing. `<= 0` = suivi immédiat permanent. |
| 15 | Entrée Trailing | `entry_trailing_retracement_pct` | float | 0.0001 – 0.1 | Après que le prix atteint son extrême, il doit rebondir de ce % pour déclencher l'entrée trailing (ex. pour les longs : baisse puis remontée de ce montant depuis le creux). |
| 16 | Entrée Trailing | `entry_trailing_double_down_factor` | float | 0.01 – 4 | Même rôle que le DDF de grille mais utilisé exclusivement pour les entrées trailing, permettant un contrôle de taille indépendant. |
| 17 | Clôture Grille | `close_grid_markup_start` | float | 0.001 – 0.03 | Premier niveau de take-profit en % de markup au-dessus (long) ou en-dessous (short) du prix d'entrée moyen. |
| 18 | Clôture Grille | `close_grid_markup_end` | float | 0.001 – 0.03 | Dernier niveau de markup TP. Les ordres sont espacés linéairement entre `start` et `end`. Si `start > end`, grille inversée (profits plus élevés clôturés en premier). |
| 19 | Clôture Grille | `close_grid_qty_pct` | float | 0.05 – 1.0 | Fraction de la position à clôturer par niveau TP. Crée environ `1 / qty_pct` ordres. `>= 1.0` = un seul ordre TP à `markup_start`. |
| 20 | Clôture Trailing | `close_trailing_grid_ratio` | float | −1 – 1 | Mélange trailing/grille pour les clôtures. Même logique que le ratio d'entrée : `0` = grille seule, `±1` = trailing seul, positif = trailing d'abord, négatif = grille d'abord. |
| 21 | Clôture Trailing | `close_trailing_qty_pct` | float | 0.05 – 1.0 | Fraction de la position à clôturer par déclenchement trailing. Plusieurs déclenchements peuvent être nécessaires pour tout clôturer. |
| 22 | Clôture Trailing | `close_trailing_threshold_pct` | float | −0.01 – 0.1 | % de profit requis pour activer le suivi trailing de clôture. `<= 0` = suivi permanent. Ex. `0.02` = trailing seulement après 2% de profit. |
| 23 | Clôture Trailing | `close_trailing_retracement_pct` | float | 0.0001 – 0.1 | Après que le prix atteint son pic de profit, il doit reculer de ce % pour déclencher la clôture. Laisse courir les profits tout en protégeant contre un retournement. |
| 24 | Unstuck | `unstuck_threshold` | float | 0.4 – 0.95 | La position est « coincée » quand `exposition / limite > seuil` (ex. `0.8` = coincée à 80%+ de l'exposition max sans sortie profitable). |
| 25 | Unstuck | `unstuck_close_pct` | float | 0.001 – 0.1 | Quantité à clôturer par ordre unstuck en fraction de la position. Petites valeurs = prise de perte graduelle sur de nombreux ordres. |
| 26 | Unstuck | `unstuck_ema_dist` | float | −0.1 – 0.01 | Distance depuis la bande EMA pour le prix de clôture unstuck. Pour les longs : `ema_band_upper × (1 + unstuck_ema_dist)`. Proche de zéro = clôtures autour de la « juste valeur ». |
| 27 | Unstuck | `unstuck_loss_allowance_pct` | float | 0.001 – 0.05 | Perte cumulée maximale que le système unstuck peut réaliser, en fraction du solde pic pondérée par TWEL. Budget de perte pour toutes les positions coincées. |
| 28 | Filtre | `filter_volume_drop_pct` | float | 0 – 1.0 | Exclure cette fraction des coins à plus faible volume. `0` = aucun filtre. Ex. `0.3` = exclure les 30% les moins échangés. |
| 29 | Filtre | `filter_volume_ema_span` | float | 10 – 1440 | Période EMA en minutes pour lisser le classement par volume. Plus long = classement plus stable, plus court = plus réactif aux changements récents. |
| 30 | Filtre | `filter_log_range_ema_span` | float | 10 – 1440 | Période EMA en minutes pour lisser le classement par volatilité log-range. Après filtrage par volume, les `n_positions` coins les plus volatils sont sélectionnés. |
| 31 | Filtre | `filter_volatility_drop_pct` | float | 0 – 1.0 | Exclure cette fraction des coins les moins volatils. `0` = aucun filtre. Complète le filtrage par volume. |
| 32 | Entrée Trailing | `entry_trailing_retracement_volatility_weight` | float | 0 – 400 | Influence de la volatilité du marché sur la distance de retracement d'entrée trailing. `0` = désactivé. |
| 33 | Entrée Trailing | `entry_trailing_retracement_we_weight` | float | 0 – 20 | Influence de l'exposition courante sur la distance de retracement d'entrée trailing. |
| 34 | Entrée Trailing | `entry_trailing_threshold_volatility_weight` | float | 0 – 400 | Influence de la volatilité du marché sur la distance du seuil d'entrée trailing. `0` = désactivé. |
| 35 | Entrée Trailing | `entry_trailing_threshold_we_weight` | float | 0 – 20 | Influence de l'exposition courante sur la distance du seuil d'entrée trailing. |
