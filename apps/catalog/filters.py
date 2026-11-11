"""Query filters for catalog listing endpoints.

Category filters expand the requested category to its whole subtree so a
parent landing page lists the products assigned to its child categories.
"""

import django_filters

from apps.catalog.models import Category, Product


class ProductFilter(django_filters.FilterSet):
    """Public product-list filters, including category descendant matching."""

    category = django_filters.NumberFilter(method="filter_category")

    class Meta:
        model = Product
        fields = ["brand", "is_featured", "product_type", "tax_class"]

    def filter_category(self, queryset, name, value):
        """Filter products to a category and all of its descendants.

        Args:
            queryset (QuerySet): the product queryset being filtered.
            name (str): the filter field name.
            value (int | None): the requested category id.

        Returns:
            QuerySet: products assigned to the category subtree.
        """
        if value is None:
            return queryset
        category_ids = self._category_and_descendant_ids(value)
        return queryset.filter(category_id__in=category_ids)

    @staticmethod
    def _category_and_descendant_ids(category_id):
        """Collect a category id plus every nested child id below it.

        Args:
            category_id (int): the root category id to expand.

        Returns:
            list[int]: the requested id followed by each descendant id.
        """
        collected = [category_id]
        frontier = [category_id]
        seen = {category_id}
        while frontier:
            children = list(
                Category.objects.filter(parent_id__in=frontier).values_list(
                    "pk", flat=True
                )
            )
            frontier = [child for child in children if child not in seen]
            seen.update(frontier)
            collected.extend(frontier)
        return collected


class AdminProductFilter(ProductFilter):
    """Admin product-list filters, adding lifecycle and stock fields."""

    class Meta(ProductFilter.Meta):
        fields = ProductFilter.Meta.fields + [
            "is_active",
            "is_discontinued",
            "stock_status",
        ]
